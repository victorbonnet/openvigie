"""Commands from the hub (D14): run only allowed actions, once, before they expire."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openvigie.adapters.base import HostContext
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST
from openvigie.collector.client import HubClient
from openvigie.collector.commands import CommandRunner
from openvigie.collector.daemon import Collector
from openvigie.collector.state import StateStore
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import CommandStatus, SandboxState

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
SECRET = "sk-ant-api03-" + "z" * 40


def command(cid: str = "a" * 32, action: str = "noop", expires: datetime = NOW + timedelta(
        minutes=5), **args: str) -> dict[str, Any]:  # fmt: skip
    return {"command_id": cid, "action": action, "expires_at": expires.isoformat(), "args": args}


def runner(allow: tuple[str, ...] = ("noop",), store: StateStore | None = None) -> CommandRunner:
    def boom(args: Any) -> str:
        raise RuntimeError(f"cannot reach api with {SECRET}")

    return CommandRunner(allow, store, actions={"noop": lambda a: "ok", "boom": boom},
                         clock=lambda: NOW)  # fmt: skip


def outcome(r: CommandRunner, item: Any) -> tuple[CommandStatus, str] | None:
    result = r.run(item)
    return None if result is None else (result.status, result.detail)


def test_allowed_action_runs() -> None:
    assert outcome(runner(), command()) == (CommandStatus.SUCCEEDED, "ok")


def test_nothing_is_allowed_by_default() -> None:
    assert CollectorSettings().allow_actions == ()
    status, detail = outcome(runner(allow=()), command()) or (None, "")
    assert status is CommandStatus.REFUSED and "allow_actions" in detail


def test_unknown_action_is_refused_even_if_allowed() -> None:
    status, _ = outcome(runner(allow=("noop", "rm")), command(action="rm")) or (None, "")
    assert status is CommandStatus.REFUSED


def test_expired_command_is_refused() -> None:
    assert outcome(runner(), command(expires=NOW)) == (CommandStatus.REFUSED, "expired")


def test_replay_is_refused() -> None:
    r = runner()
    assert outcome(r, command())[0] is CommandStatus.SUCCEEDED  # type: ignore[index]
    assert outcome(r, command()) == (CommandStatus.REFUSED, "already received (replay)")


def test_replay_is_refused_across_restarts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    assert outcome(runner(store=store), command())[0] is CommandStatus.SUCCEEDED  # type: ignore[index]
    store.close()
    again = runner(store=StateStore(tmp_path / "state.db"))
    assert outcome(again, command())[0] is CommandStatus.REFUSED  # type: ignore[index]


def test_failure_is_reported_redacted() -> None:
    status, detail = outcome(runner(allow=("boom",)), command(action="boom")) or (None, "")
    assert status is CommandStatus.FAILED and "RuntimeError" in detail and SECRET not in detail


def test_malformed_commands() -> None:
    r = runner()
    assert outcome(r, command(action="Bad Action")) == (CommandStatus.REFUSED, "malformed command")
    assert outcome(r, {"nonsense": True}) is None
    assert outcome(r, "x") is None
    assert r.run_all("not a list") == [] and r.run_all(None) == []


# --- end to end: hub queue → ingest reply → collector → result → audit ---


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Collector, Database]:
    db = Database(MEMORY)
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    Inventory(db).upsert_host(TEST_HOST, datetime.now(UTC))
    token = Credentials(db).issue_token(TEST_HOST.host_id, datetime.now(UTC))

    def transport(method: str, url: str, body: Any, headers: Any, timeout: float) -> Any:
        response = client.request(method, url.split("7878", 1)[1], content=body, headers=headers)
        return response.status_code, response.content

    ctx = HostContext(host=TEST_HOST, homes=(tmp_path,), process_source=list,
                      sandbox_probe=lambda pid: (SandboxState.NO, None))  # fmt: skip
    collector = Collector(CollectorSettings(homes=(tmp_path,), allow_actions=("noop",)), ctx,
                          Discovery(adapters=[]),
                          HubClient("http://127.0.0.1:7878", token=token, transport=transport),
                          store=StateStore(tmp_path / "state.db"))  # fmt: skip
    return collector, db


def queue(db: Database, action: str = "noop", ttl: timedelta = timedelta(minutes=5)) -> str:
    return CommandQueue(db).enqueue(TEST_HOST.host_id, action, {"note": "hi"}, actor="alice",
                                    now=datetime.now(UTC), ttl=ttl).command_id  # fmt: skip


def test_noop_round_trip_is_audited(setup: tuple[Collector, Database]) -> None:
    collector, db = setup
    cid = queue(db)
    assert collector.tick()  # reply carries the command; the collector runs it
    stored = CommandQueue(db).get(cid)
    assert stored is not None and stored.status == "delivered"
    assert collector.tick()  # the result goes out with this push
    stored = CommandQueue(db).get(cid)
    assert stored is not None and stored.status == "succeeded"
    assert collector.command_results == []
    assert collector.tick()  # delivered once only
    log = AuditLog(db)
    assert [(e.action, e.result, e.request_id) for e in reversed(log.entries())] == [
        ("command.noop", "delivered", cid),
        ("command.noop", "succeeded", cid),
    ]
    assert log.verify().ok


def test_refused_on_the_collector_is_reported(setup: tuple[Collector, Database]) -> None:
    collector, db = setup
    cid = queue(db, action="sandbox.policy.apply")
    collector.tick(), collector.tick()
    stored = CommandQueue(db).get(cid)
    assert stored is not None and (stored.status, stored.detail) == (
        "refused", "action not allowed on this host (allow_actions)")  # fmt: skip


def test_undelivered_commands_expire_on_the_hub(setup: tuple[Collector, Database]) -> None:
    collector, db = setup
    cid = queue(db, ttl=timedelta(seconds=-1))
    collector.tick()
    stored = CommandQueue(db).get(cid)
    assert stored is not None and stored.status == "expired"
    assert [e.result for e in AuditLog(db).entries()] == ["expired"]


def test_results_for_other_hosts_commands_are_ignored(setup: tuple[Collector, Database]) -> None:
    collector, db = setup
    other = CommandQueue(db).enqueue("other-host", "noop", {}, actor="alice",
                                     now=datetime.now(UTC), ttl=timedelta(minutes=5))  # fmt: skip
    collector.command_results.append({"command_id": other.command_id, "status": "succeeded",
                                      "finished_at": NOW.isoformat(), "detail": ""})  # fmt: skip
    collector.tick()
    stored = CommandQueue(db).get(other.command_id)
    assert stored is not None and stored.status == "queued"
