import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.capabilities import CapabilityStore
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import (
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    Host,
    McpServer,
    NetAccess,
    SandboxState,
)

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.1.0")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.1.0")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    inv = Inventory(db)
    for h in (PI, MAC):
        inv.upsert_host(h, NOW)
    inv.upsert_agents([
        AgentInstance("calm", "h-pi", "codex", "Calm", sandboxed=SandboxState.YES),
        AgentInstance("wild", "h-pi", "claude-code", "Wild", sandboxed=SandboxState.NO),
        AgentInstance("mac", "h-mac", "claude-code", "Mac agent", sandboxed=SandboxState.NO),
        AgentInstance("blind", "h-pi", "codex", "Blind"),
    ], NOW)  # fmt: skip
    CapabilityStore(db).put([
        Capabilities("calm", NOW, ApprovalLevel.ASK, network=NetAccess.NONE, shell=True),
        Capabilities("wild", NOW, ApprovalLevel.NEVER_ASK, "bypassPermissions",
                     mcp_servers=(McpServer("<b>gh</b>", "stdio", "npx", pinned=False),)),
        Capabilities("mac", NOW, ApprovalLevel.ASK, network=NetAccess.NONE, shell=True),
    ], "h-pi")  # fmt: skip
    CapabilityStore(db).put([Capabilities("mac", NOW, ApprovalLevel.ASK, network=NetAccess.NONE,
                                          shell=True)], "h-mac")  # fmt: skip
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def levels(html: str) -> list[tuple[str, str]]:
    rows = re.findall(r'<a href="/agents/(\w+)">.*?class="badge risk-(\w+)"', html, flags=re.S)
    return [(a, lvl) for a, lvl in rows]


def test_agent_list_risk_column_and_sort(client: TestClient) -> None:
    html = client.get("/").text
    assert dict(levels(html)) == {"calm": "none", "wild": "high", "mac": "low",
                                  "blind": "unknown"}  # fmt: skip
    assert '<span aria-hidden="true">▲</span> high' in html  # icon + text, not color alone
    by_risk = [a for a, _ in levels(client.get("/?sort=risk").text)]
    assert by_risk == ["wild", "mac", "blind", "calm"]
    assert [a for a, _ in levels(client.get("/?sort=-risk").text)] == ["calm", "blind", "mac",
                                                                      "wild"]  # fmt: skip
    rows = client.get("/agents/rows?sort=risk&type=codex").text
    assert [a for a, _ in levels(rows)] == ["blind", "calm"]


def test_agent_page_lists_flags_with_reasons(client: TestClient) -> None:
    html = client.get("/agents/wild").text
    assert "Runs anything without asking, outside a sandbox" in html
    assert "(bypassPermissions)" in html and "rule <code>never-asks-unsandboxed</code>" in html
    assert "&lt;b&gt;gh&lt;/b&gt;" in html and "<b>gh</b>" not in html  # reasons are escaped
    assert "risk can't be assessed" in client.get("/agents/blind").text


def test_macos_note_and_host_counts(client: TestClient) -> None:
    assert "can't be sandboxed with OpenShell" in client.get("/agents/mac").text
    assert "can't be sandboxed with OpenShell" not in client.get("/agents/wild").text
    hosts = client.get("/hosts").text
    assert "1 high" in hosts and "no high-risk agents" in hosts


def test_broken_rules_are_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "risk.toml").write_text("[[rules]]\nid = 'x'\n")
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    Inventory(db).upsert_agents([AgentInstance("a", "h-pi", "codex", "A")], NOW)
    html = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878").get("/").text
    assert "Risk rules:" in html and "The bundled rules are in use." in html
