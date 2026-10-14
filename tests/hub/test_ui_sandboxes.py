"""OpenShell sandboxes in the web UI (FR-10)."""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.model import (
    AgentInstance,
    Host,
    OpenShellSandbox,
    SandboxEndpoint,
    SandboxRule,
    SandboxState,
)

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.1.0")
AGENT = AgentInstance("a1", "h-pi", "claude-code", "Claude Code", sandboxed=SandboxState.YES,
                      sandbox_kind="openshell:ov-net")  # fmt: skip
NET = OpenShellSandbox(
    "s-net", "h-pi", "ov-net", NOW, "default", "Ready", {"openvigie.agent": "claude-code"},
    "claude-code", ("a1",), 3, "4b27e5bfba0ff9cd",
    (SandboxRule("github_readonly", (SandboxEndpoint("api.github.com", (443,), None, "rest",
                                                     "read-only", "enforce"),), ("/usr/bin/curl",)),
     SandboxRule("docs", (SandboxEndpoint("docs.example.com", (443,), None, "rest", "read-only",
                                          None),), ("/usr/bin/curl",)),
     SandboxRule("<b>x</b>", (), ())),
)  # fmt: skip
EMPTY = OpenShellSandbox("s-empty", "h-pi", "ov-closed", NOW, phase="Provisioning",
                         network_rules=())  # fmt: skip
UNKNOWN = OpenShellSandbox("s-unknown", "h-pi", "ov-unknown", NOW, phase="Ready")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    Inventory(db).upsert_agents([AGENT], NOW)
    store = SandboxStore(db)
    store.replace("h-pi", [replace(NET, policy_version=2), EMPTY, UNKNOWN], NOW)
    store.replace("h-pi", [NET, EMPTY, UNKNOWN], NOW)  # a policy update: revision 2 → 3
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def test_agent_list_and_status_name_the_sandbox(client: TestClient) -> None:
    assert "OpenShell: ov-net · policy r3" in client.get("/").text
    assert "OpenShell: ov-net · policy r3" in client.get("/agents/rows").text
    assert "sandboxed: OpenShell: ov-net" in client.get("/agents/a1/status").text


def test_agent_page_shows_its_sandbox_rules_and_history(client: TestClient) -> None:
    html = client.get("/agents/a1").text
    assert "<h2>OpenShell sandbox</h2>" in html and "revision 3" in html
    assert "<code>api.github.com:443</code> rest · read-only" in html
    assert html.count("audit only") == 1  # docs.example.com: no enforcement = OpenShell's audit
    assert "policy revision: 2 → 3" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html and "<b>x</b>" not in html
    assert "no binaries: allows nothing" in html


def test_host_page_lists_every_sandbox(client: TestClient) -> None:
    html = client.get("/hosts/h-pi").text
    assert html.count('class="panel" id="sandbox-') == 3
    assert "No network access: OpenShell denies every connection" in html
    assert "Unknown: the collector couldn't read this sandbox's policy" in html
    assert '<a href="/agents/a1">Claude Code</a>' in html
    assert "none detected" in html


def test_pages_without_openshell_are_unchanged(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:  # fmt: skip
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    Inventory(db).upsert_agents([replace(AGENT, sandboxed=SandboxState.NO, sandbox_kind=None)],
                                NOW)  # fmt: skip
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    assert "OpenShell" not in client.get("/hosts/h-pi").text
    assert "OpenShell sandbox" not in client.get("/agents/a1").text
