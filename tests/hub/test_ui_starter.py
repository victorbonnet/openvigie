"""The starter OpenShell policy on an agent's page, and its download (FR-11)."""

import html
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.capabilities import CapabilityStore
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.model import (
    AgentInstance,
    Capabilities,
    Host,
    McpServer,
    OpenShellSandbox,
    SandboxEndpoint,
    SandboxRule,
    SandboxState,
)
from openvigie.openshell.starter import generate

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.1.0")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.1.0")
AGENT = AgentInstance("a1", "h-pi", "claude-code", "<b>Claude</b>", sandboxed=SandboxState.YES,
                      sandbox_kind="openshell:ov")  # fmt: skip
SERVERS = (McpServer("docs", "http", None, "mcp.example.com"), McpServer("github", "stdio", "npx"))
CAPS = Capabilities("a1", NOW, mcp_servers=SERVERS)
GITHUB = SandboxRule("github", (SandboxEndpoint("api.github.com", (443,)),), ("/usr/bin/curl",))
SANDBOX = OpenShellSandbox("s1", "h-pi", "ov", NOW, phase="Ready", agent_ids=("a1",),
                           policy_version=1, network_rules=(GITHUB,))  # fmt: skip


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    for host in (PI, MAC):
        Inventory(db).upsert_host(host, NOW)
    Inventory(db).upsert_agents([AGENT, AgentInstance("m1", "h-mac", "codex", "Codex"),
                                 AgentInstance("x1", "h-pi", "hermes", "Hermes")], NOW)  # fmt: skip
    CapabilityStore(db).put([CAPS], "h-pi")
    SandboxStore(db).replace("h-pi", [SANDBOX], NOW)
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def test_agent_page_shows_the_policy_and_how_it_differs(client: TestClient) -> None:
    page = client.get("/agents/a1").text
    assert "<h2>Starter OpenShell policy</h2>" in page and "openvigie doesn't apply it" in page
    assert "<b>Claude</b>" not in page  # the agent name inside the YAML comment is escaped
    expected = generate(AGENT, CAPS).yaml
    assert html.escape(expected, quote=False) in page.replace("&#34;", '"').replace("&#39;", "'")
    assert "the starter allows <code>api.anthropic.com</code>" in page
    assert "the sandbox also allows <code>api.github.com</code>" in page
    assert '<script src="/static/copy.js" defer></script>' in page


def test_download_is_the_same_text(client: TestClient) -> None:
    response = client.get("/agents/a1/starter-policy.yaml")
    assert response.status_code == 200 and response.text == generate(AGENT, CAPS).yaml
    assert response.headers["content-disposition"] == (
        'attachment; filename="openvigie-starter-claude-code.yaml"')  # fmt: skip
    assert response.headers["content-type"].startswith("application/yaml")
    assert client.get("/agents/nope/starter-policy.yaml").status_code == 404


def test_macos_agents_get_the_vm_note_instead(client: TestClient) -> None:
    page = client.get("/agents/m1").text
    assert "Starter OpenShell policy" not in page and "Linux VM or container" in page


def test_unknown_agent_types_still_get_a_policy_with_a_note(client: TestClient) -> None:
    page = client.get("/agents/x1").text
    assert "network_policies: {}" in page and "which hosts hermes needs" in page


def test_copy_script_is_served_under_the_csp(client: TestClient) -> None:
    script = client.get("/static/copy.js")
    assert script.status_code == 200 and "data-copy" in script.text
