"""End-to-end: real hub over HTTP on loopback, real enrolment, collector on a fake host."""

import os
import re
import shutil
import threading
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn

from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST, fake_process
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.collector.enrol import enrol
from openvigie.config import CollectorSettings, HubSettings, read_private_file
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import Database
from openvigie.model import SandboxState

pytestmark = pytest.mark.e2e

ADAPTERS = Path(__file__).parents[1] / "adapters"


class Hub:
    def __init__(self, tmp_path: Path) -> None:
        self.db = Database(tmp_path / "hub" / "hub.db")
        app = create_app(HubSettings(), self.db)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "Hub":
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("hub did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.db.close()

    def get(self, path: str) -> str:
        with urllib.request.urlopen(self.url + path, timeout=5) as response:  # noqa: S310
            return str(response.read().decode())


class FakeMachine:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        shutil.copytree(ADAPTERS / "claude_code/fixtures/home/.claude", self.home / ".claude")
        shutil.copytree(ADAPTERS / "codex/fixtures/home/.codex", self.home / ".codex")
        self.now = datetime.now(UTC)
        self.processes: list[ProcessInfo] = []
        self.touch(".claude", self.now - timedelta(hours=2))
        self.touch(".codex", self.now - timedelta(hours=2))

    def touch(self, root: str, when: datetime) -> None:
        for path in (self.home / root).rglob("*.jsonl"):
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
def hub(tmp_path: Path) -> Iterator[Hub]:
    with Hub(tmp_path) as running:
        yield running


def statuses(html: str) -> dict[str, str]:
    """Map agent name to status from the agent list HTML."""
    found = {}
    for row in re.findall(r"<tr>(.*?)</tr>", html, flags=re.S):
        name = re.search(r'href="/agents/[^"]+">([^<]+)</a>', row)
        status = re.search(r'<span class="badge (\w+)">\1</span>', row)
        if name and status:
            found[name.group(1)] = status.group(1)
    return found


def test_scan_enrol_collect_and_see_status_change(hub: Hub, tmp_path: Path) -> None:
    machine = FakeMachine(tmp_path)
    settings = CollectorSettings(homes=(machine.home,), token_file=tmp_path / "state" / "token")

    code = Credentials(hub.db).create_enrol_code(datetime.now(UTC), timedelta(minutes=5))
    token_file = enrol(hub.url, code, TEST_HOST, settings, config_path=tmp_path / "c.toml")
    client = HubClient(hub.url, token=read_private_file(token_file))
    discovery = Discovery(adapters=[ClaudeCodeAdapter(), CodexAdapter()])
    collector = Collector(settings, machine.context(), discovery, client)

    assert collector.tick()
    assert statuses(hub.get("/")) == {"Claude Code": "stopped", "Codex": "stopped"}

    started = time.monotonic()
    machine.processes = [fake_process(97, "claude"), fake_process(300, "codex")]
    machine.touch(".claude", machine.now)
    assert collector.tick()
    assert statuses(hub.get("/agents/rows")) == {"Claude Code": "active", "Codex": "idle"}
    assert time.monotonic() - started < 10  # freshness budget, one tick end to end

    hosts = hub.get("/hosts")
    assert "testhost" in hosts and "stale" not in hosts
