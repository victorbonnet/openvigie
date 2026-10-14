import os
import shutil
import threading
import time
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
from openvigie.model import AgentStatus, SandboxState

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
