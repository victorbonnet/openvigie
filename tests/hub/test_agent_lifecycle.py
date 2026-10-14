"""Stopping and killing an agent from its page (FR-13, D16), end to end with a real process."""

import re
import subprocess
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvigie.adapters.base import Adapter, HostContext, ProcessInfo
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import AgentInstance, SandboxState

TS_NAME = "hub.tail1234.ts.net"
SETTINGS = HubSettings(allowed_hosts=("127.0.0.1", TS_NAME), admins=("alice@example.com",),
                       trust_tailscale_identity=True)  # fmt: skip
ALICE = {"Tailscale-User-Login": "alice@example.com"}
BOB = {"Tailscale-User-Login": "bob@example.com"}
CSRF = re.compile(r'action="([^"]+)"[^<]*<input type="hidden" name="csrf" value="([0-9a-f]+)"')


class FakeAdapter(Adapter):
    """One agent, running as the given process."""

    agent_type = "fake"
    display_name = "Fake Agent"

    def __init__(self, proc: subprocess.Popen[Any]) -> None:
        super().__init__()
        p = psutil.Process(proc.pid)
        self.info = ProcessInfo(proc.pid, 1, p.name(), "", None, p.create_time(), ())
        self.proc = proc

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        return [AgentInstance("a1", TEST_HOST.host_id, "fake", "Fake Agent",
                              process_running=self.proc.poll() is None)]  # fmt: skip

    def processes(self, agent_id: str) -> Sequence[ProcessInfo]:
        return (self.info,) if agent_id == "a1" and self.proc.poll() is None else ()


@pytest.fixture
def agent_proc() -> Iterator[subprocess.Popen[Any]]:
    proc = subprocess.Popen(["sleep", "60"])  # noqa: S607
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


@pytest.fixture
def app() -> FastAPI:
    db = Database(MEMORY)
    Inventory(db).upsert_host(TEST_HOST, datetime.now(UTC))
    return create_app(SETTINGS, db)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"https://{TS_NAME}", client=("127.0.0.1", 50000),
                      follow_redirects=False)  # fmt: skip


def collector_for(app: FastAPI, client: TestClient, adapter: Adapter, tmp: Path) -> Collector:
    token = Credentials(app.state.db).issue_token(TEST_HOST.host_id, datetime.now(UTC))

    def transport(method: str, url: str, body: Any, headers: Any, timeout: float) -> Any:
        response = client.request(method, url.split("7878", 1)[1], content=body, headers=headers)
        return response.status_code, response.content

    ctx = HostContext(host=TEST_HOST, homes=(tmp,), process_source=list,
                      sandbox_probe=lambda pid: (SandboxState.NO, None))  # fmt: skip
    settings = CollectorSettings(homes=(tmp,), allow_actions=("agent.stop",))
    hub = HubClient("http://127.0.0.1:7878", token=token, transport=transport)
    return Collector(settings, ctx, Discovery(adapters=[adapter]), hub)


def propose(client: TestClient, kind: str) -> Any:
    forms = dict(CSRF.findall(client.get("/agents/a1", headers=ALICE).text))
    data = {"csrf": forms["/actions/new"], "kind": kind, "agent_id": "a1"}
    return client.post("/actions/new", data=data, headers=ALICE)


def confirm(client: TestClient, location: str, typed: str) -> Any:
    forms = dict(CSRF.findall(client.get(location, headers=ALICE).text))
    token = forms[f"{location}/confirm"]
    return client.post(f"{location}/confirm", data={"csrf": token, "typed": typed}, headers=ALICE)


def test_stop_end_to_end(
    app: FastAPI, client: TestClient, agent_proc: subprocess.Popen[Any], tmp_path: Path
) -> None:
    collector = collector_for(app, client, FakeAdapter(agent_proc), tmp_path)
    assert collector.tick()  # the hub learns the agent and that it runs
    page = client.get("/agents/a1", headers=ALICE).text
    assert "Stop…" in page and "Kill…" in page
    assert "Stop…" not in client.get("/agents/a1", headers=BOB).text

    created = propose(client, "agent.stop")
    assert created.status_code == 303
    location = created.headers["location"]
    review = client.get(location, headers=ALICE).text
    assert "Stop Fake Agent on testhost" in review and "<code>testhost/fake</code>" in review
    assert "Propose a rollback" not in review
    assert confirm(client, location, "testhost/fake").status_code == 303
    assert collector.tick() and collector.tick()
    assert agent_proc.poll() is not None
    page = client.get(location, headers=ALICE).text
    assert "succeeded" in page and "stopped 1 process" in page
    assert [e.result for e in reversed(AuditLog(app.state.db).entries())] == [
        "proposed", "confirmed", "delivered", "succeeded"]  # fmt: skip

    collector.tick()  # the agent is no longer running: no buttons, no proposal
    assert "Stop…" not in client.get("/agents/a1", headers=ALICE).text


def test_kill_refused_by_a_host_that_only_allows_stop(
    app: FastAPI, client: TestClient, agent_proc: subprocess.Popen[Any], tmp_path: Path
) -> None:
    collector = collector_for(app, client, FakeAdapter(agent_proc), tmp_path)
    collector.tick()
    location = propose(client, "agent.kill").headers["location"]
    assert confirm(client, location, "testhost/fake").status_code == 303
    collector.tick(), collector.tick()
    assert agent_proc.poll() is None
    assert "action not allowed on this host" in client.get(location, headers=ALICE).text
