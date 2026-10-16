import json
import os
import shutil
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openvigie import cli
from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST, fake_process
from openvigie.collector.client import HubClient, HubError
from openvigie.collector.daemon import Collector, rss_mib
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.model import AgentStatus, HealthState, SandboxState, to_dict
from openvigie.openshell.cli import Reader
from openvigie.openshell.mapping import OpenShellProbe

FIXTURE = Path(__file__).parents[1] / "adapters/claude_code/fixtures/home/.claude"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class FakeHost:
    """A mutable fake machine: tests start and stop processes and move time."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        shutil.copytree(FIXTURE, self.home / ".claude")
        self.now = datetime.now(UTC)
        self.processes: list[ProcessInfo] = []
        self.touch(self.now - timedelta(hours=1))

    def touch(self, when: datetime) -> None:
        for path in (self.home / ".claude").rglob("*.jsonl"):
            os.utime(path, (when.timestamp(), when.timestamp()))

    def context(self) -> HostContext:
        return HostContext(
            host=TEST_HOST,
            homes=(self.home,),
            process_source=lambda: list(self.processes),
            sandbox_probe=lambda pid: (SandboxState.NO, None),
            clock=lambda: self.now,
        )


@pytest.fixture
def hub() -> tuple[TestClient, Database]:
    db = Database(MEMORY)
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878"), db


class Transport:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.down = False
        self.calls = 0

    def __call__(self, method: str, url: str, body: Any, headers: Any, timeout: float) -> Any:
        self.calls += 1
        if self.down:
            raise HubError(0, "cannot reach the hub: connection refused")
        response = self.client.request(method, url.split("7878", 1)[1], content=body,
                                       headers=headers)  # fmt: skip
        return response.status_code, response.content


def make_collector(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> tuple[Collector, FakeHost, Transport, Clock]:
    client, db = hub
    Inventory(db).upsert_host(TEST_HOST, datetime.now(UTC))
    token = Credentials(db).issue_token(TEST_HOST.host_id, datetime.now(UTC))
    host = FakeHost(tmp_path)
    transport = Transport(client)
    clock = Clock()
    collector = Collector(
        CollectorSettings(homes=(host.home,)),
        host.context(),
        Discovery(adapters=[ClaudeCodeAdapter()]),
        HubClient("http://127.0.0.1:7878", token=token, transport=transport),
        monotonic=clock,
    )
    return collector, host, transport, clock


def hub_status(db: Database) -> AgentStatus:
    (row,) = Inventory(db).agents()
    return row.agent.status


def test_status_change_reaches_hub_within_one_tick(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector, host, _, clock = make_collector(tmp_path, hub)
    assert collector.tick()
    assert hub_status(hub[1]) is AgentStatus.STOPPED

    host.processes = [fake_process(97, "claude")]
    clock.t += 5
    assert collector.tick()
    assert hub_status(hub[1]) is AgentStatus.IDLE

    host.touch(host.now)
    clock.t += 5
    assert collector.tick()
    assert hub_status(hub[1]) is AgentStatus.ACTIVE
    (row,) = Inventory(hub[1]).agents()
    assert [e.new for e in Inventory(hub[1]).status_history(row.agent.agent_id)] == [
        AgentStatus.ACTIVE,
        AgentStatus.IDLE,
        AgentStatus.STOPPED,
    ]


def test_only_changes_are_pushed_between_heartbeats(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector, _, _, clock = make_collector(tmp_path, hub)
    collector.tick()
    clock.t += 5
    agents, health = collector.observe()
    payload = collector.build_payload(agents, health, full=False)
    assert payload["agents"] == [] and payload["adapter_health"] == []
    assert collector.build_payload(agents, health, full=True)["agents"]


def test_hub_down_then_recovers_without_losing_events(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector, host, transport, clock = make_collector(tmp_path, hub)
    transport.down = True
    assert collector.tick() is False
    host.processes = [fake_process(97, "claude")]
    clock.t += 1  # still inside the backoff window: no push attempted
    calls = transport.calls
    assert collector.tick() is False
    assert transport.calls == calls
    assert len(collector.state.pending_events) == 2  # stopped, then idle

    transport.down = False
    clock.t += 120
    assert collector.tick() is True
    assert hub_status(hub[1]) is AgentStatus.IDLE
    assert not collector.state.pending_events


def test_revoked_token_backs_off_for_minutes(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector, _, transport, clock = make_collector(tmp_path, hub)
    collector.client.token = "ovh_revoked"  # noqa: S105
    assert collector.tick() is False
    assert collector.state.unauthorized
    calls = transport.calls
    clock.t += 60
    collector.tick()
    assert transport.calls == calls


def test_run_loop_stops_on_event(tmp_path: Path, hub: tuple[TestClient, Database]) -> None:
    collector, _, transport, _ = make_collector(tmp_path, hub)
    collector.monotonic = time.monotonic
    collector.settings = CollectorSettings(fast_tick_s=0.01)
    stop = threading.Event()
    thread = threading.Thread(target=collector.run, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 10
    while transport.calls < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_rss_is_reported() -> None:
    assert 1 < rss_mib() < 4096


def test_cli_collect_requires_enrolment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OPENVIGIE_STATE_DIR", str(tmp_path / "state"))
    assert cli.main(["collect", "--once"]) == 2
    assert "openvigie enrol" in capsys.readouterr().err


def test_usage_flows_to_hub_once(tmp_path: Path, hub: tuple[TestClient, Database]) -> None:
    from openvigie.collector.state import StateStore
    from openvigie.hub.store.usage import UsageStore

    collector, _, transport, clock = make_collector(tmp_path, hub)
    collector.store = StateStore(tmp_path / "state" / "collector.db")
    assert collector.tick()
    records = UsageStore(hub[1]).records()
    assert len(records) == 9
    assert sum(r.output_tokens or 0 for r in records) == 24009
    calls = transport.calls
    clock.t += collector.settings.slow_tick_s
    collector.tick()
    assert len(UsageStore(hub[1]).records()) == 9
    assert transport.calls == calls + 1  # inventory only: nothing new to send


def test_usage_waits_while_hub_is_down(tmp_path: Path, hub: tuple[TestClient, Database]) -> None:
    from openvigie.collector.state import StateStore
    from openvigie.hub.store.usage import UsageStore

    collector, _, transport, clock = make_collector(tmp_path, hub)
    collector.store = StateStore(tmp_path / "state" / "collector.db")
    transport.down = True
    collector.tick()
    assert collector.store.load_cursors("claude-code") == {}  # nothing read, nothing lost
    transport.down = False
    clock.t += 120
    assert collector.tick()
    assert len(UsageStore(hub[1]).records()) == 9


def test_spooled_usage_is_flushed_first(tmp_path: Path, hub: tuple[TestClient, Database]) -> None:
    from openvigie.collector.state import StateStore
    from openvigie.hub.store.usage import UsageStore

    collector, _, _, _ = make_collector(tmp_path, hub)
    collector.store = StateStore(tmp_path / "state" / "collector.db")
    collector.tick()  # agents and their usage reach the hub
    record = UsageStore(hub[1]).records()[0]
    UsageStore(hub[1]).db.conn.execute("DELETE FROM usage_records")
    collector.store.spool_put(collector.usage_payload([to_dict(record)], []))
    collector.last_slow = float("-inf")
    collector.tick()
    assert [r.record_id for r in UsageStore(hub[1]).records()] == [record.record_id]
    assert collector.store.spool_stats()[0] == 0


def test_span_chunks_respect_count_and_size() -> None:
    from openvigie.collector import daemon

    small = [{"n": i} for i in range(daemon.SPAN_CHUNK + 5)]
    assert [len(c) for c in daemon._span_chunks(small)] == [daemon.SPAN_CHUNK, 5]
    big = [{"text": "x" * (daemon.SPAN_CHUNK_BYTES // 3)} for _ in range(7)]
    chunks = daemon._span_chunks(big)
    assert sum(len(c) for c in chunks) == 7 and all(len(c) <= 2 for c in chunks)
    assert daemon._span_chunks([]) == []


# --- OpenShell (FR-10) ------------------------------------------------------------------------

OPENSHELL = Path(__file__).parents[1] / "openshell" / "fixtures" / "v0.1.4"
GET = json.loads((OPENSHELL / "sandbox-get.json").read_text())


def fake_openshell(version: str = "openshell 0.1.4") -> Reader:
    def run(args: Sequence[str]) -> str:
        if list(args) == ["--version"]:
            return version
        if list(args[:2]) == ["sandbox", "list"]:
            return json.dumps({"sandboxes": [GET], "next_page_token": ""})
        if list(args[:2]) == ["sandbox", "get"]:
            return json.dumps(GET)
        return (OPENSHELL / "policy-base.json").read_text()

    return Reader(run)


def sandboxed_collector(
    tmp_path: Path, hub: tuple[TestClient, Database], reader: Reader
) -> Collector:
    """A collector whose Claude Code process is the main process of the recorded sandbox."""
    collector, host, _, _ = make_collector(tmp_path, hub)
    started = GET["main_process_started_at_ms"] / 1000
    host.processes = [
        fake_process(2835, "openshell-sandbox", ppid=1),
        fake_process(2955, "claude", ["claude"], ppid=2835, create_time=started),
    ]
    collector.ctx.sandbox_probe = OpenShellProbe(lambda pid: (SandboxState.NO, None),
                                                 lambda: list(host.processes))  # fmt: skip
    collector.openshell = reader
    return collector


def test_agent_in_a_sandbox_gets_its_name(tmp_path: Path, hub: tuple[TestClient, Database]) -> None:
    collector = sandboxed_collector(tmp_path, hub, fake_openshell())
    collector.tick()  # the slow tick reads OpenShell
    agents, health = collector.observe()
    assert [(a.sandboxed, a.sandbox_kind) for a in agents] == [
        (SandboxState.YES, "openshell:ov-fixture")]  # fmt: skip
    assert [(h.adapter, h.state) for h in health if h.adapter == "openshell"] == [
        ("openshell", HealthState.OK)]  # fmt: skip


def test_unsupported_openshell_is_reported_as_health(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector = sandboxed_collector(tmp_path, hub, fake_openshell("openshell 0.1.2"))
    collector.tick()
    agents, health = collector.observe()
    (row,) = [h for h in health if h.adapter == "openshell"]
    assert row.state is HealthState.DEGRADED and "0.1.4 or newer" in (row.reason or "")
    assert agents[0].sandboxed is SandboxState.NO  # falls back to the generic probe


def test_sandboxes_reach_the_hub_with_their_agents(
    tmp_path: Path, hub: tuple[TestClient, Database]
) -> None:
    collector = sandboxed_collector(tmp_path, hub, fake_openshell())
    clock = collector.monotonic
    assert isinstance(clock, Clock)
    collector.tick()  # reads OpenShell; the agent isn't placed yet when it was detected
    clock.t += 31
    collector.tick()  # detected inside the sandbox, so the sandbox now lists it
    (box,) = SandboxStore(hub[1]).all()
    (agent,) = Inventory(hub[1]).agents()
    assert box.name == "ov-fixture" and box.agent_ids == (agent.agent.agent_id,)
    assert agent.agent.sandbox_kind == "openshell:ov-fixture"
    sent_at = collector.sandboxes_sent_at
    clock.t += 31
    collector.tick()
    assert collector.sandboxes_sent_at == sent_at  # unchanged, so not sent again


# --- reading history (FR-3, FR-5) --------------------------------------------------------------


def test_history_larger_than_a_tick_shows_reading_then_complete(
    tmp_path: Path, hub: tuple[TestClient, Database], monkeypatch: pytest.MonkeyPatch
) -> None:
    from openvigie.adapters.usage import TailBudget
    from openvigie.hub.store.usage import UsageStore

    # about one transcript per tick: the fixture's two transcripts take two ticks to read
    monkeypatch.setattr(TailBudget, "for_context", classmethod(lambda cls, ctx: cls(1, 1)))
    collector, host, transport, clock = make_collector(tmp_path, hub)
    client, db = hub
    assert collector.tick()
    (row,) = Inventory(db).agents()
    agent_id = row.agent.agent_id
    state = UsageStore(db).backfill(agent_id)
    assert state is not None and not state.complete and state.bytes_left
    assert state.read_until is not None  # the unread transcript's last write
    assert abs(state.read_until - (host.now - timedelta(hours=1))) < timedelta(seconds=1)
    page = client.get("/usage").text
    assert "Claude Code · testhost: reading history, up to" in page
    assert "≥ " in page and "clears by itself" in page
    assert "reading history" in client.get(f"/agents/{agent_id}").text
    assert client.get("/api/v1/usage").json()["total"]["history_complete"] is False

    clock.t += collector.settings.slow_tick_s
    assert collector.tick()
    state = UsageStore(db).backfill(agent_id)
    assert state is not None and state.complete and state.read_until is None
    assert len(UsageStore(db).records()) == 9  # all of it, as with an unbounded read
    page = client.get("/usage").text
    assert "reading history" not in page and "clears by itself" not in page
    assert "reading history" not in client.get(f"/agents/{agent_id}").text
    usage = client.get("/api/v1/usage").json()
    assert usage["reading"] == [] and usage["total"]["history_complete"] is True

    calls = transport.calls
    clock.t += collector.settings.slow_tick_s
    collector.tick()
    assert transport.calls == calls + 1  # complete and unchanged: nothing more to send
