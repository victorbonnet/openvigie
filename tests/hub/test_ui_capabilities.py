from datetime import UTC, datetime, timedelta

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
    Channel,
    FsAccess,
    Hook,
    Host,
    McpServer,
    NetAccess,
    SecretRef,
)

NOW = datetime.now(UTC).replace(microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
FULL = Capabilities(
    "a1", NOW - timedelta(hours=2), ApprovalLevel.ASK, "default", FsAccess.WORKSPACE,
    writable_roots=("~/shared",), network=NetAccess.ALLOWLIST, network_allowlist=("github.com",),
    shell=True, tools_allowed=("Bash(npm test:*)",), tools_denied=("WebFetch",),
    mcp_servers=(McpServer("github", "stdio", "npx", pinned=False, secret_names=("GH_TOKEN",)),
                 McpServer("docs", "http", url_host="mcp.example.com")),
    skills=("pdf",), hooks=(Hook("PreToolUse", "audit-bash"),),
    secrets=(SecretRef("GH_TOKEN", "user mcp:github"),), sources=("~/.claude/settings.json",),
)  # fmt: skip


def page(*snapshots: Capabilities) -> str:
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, NOW)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "claude-code", "Claude Code")], NOW)
    if snapshots:
        CapabilityStore(db).put(list(snapshots), "h-pi")
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    return client.get("/agents/a1").text


def test_full_snapshot() -> None:
    html = page(FULL)
    assert "<h2>Capabilities</h2>" in html and "<code>~/.claude/settings.json</code>" in html
    assert "asks before edits and commands" in html and "<code>default</code>" in html
    assert "project folders only, plus <code>~/shared</code>" in html
    assert "allowlisted hosts only: github.com" in html
    assert "<code>Bash(npm test:*)</code>" in html and "<code>WebFetch</code>" in html
    assert "PreToolUse → <code>audit-bash</code>" in html
    assert '<span class="badge degraded">no</span>' in html  # github MCP server is unpinned
    assert "mcp.example.com" in html and "GH_TOKEN" in html


def test_unknown_is_shown_as_unknown_and_nothing_reported_yet() -> None:
    html = page(Capabilities("a1", NOW))
    assert '<span class="badge unknown">unknown</span>' in html
    assert "<dt>Shell commands</dt><dd>unknown</dd>" in html
    assert "Not reported yet" in page()


def test_change_history() -> None:
    bypass = Capabilities(**{**{f: getattr(FULL, f) for f in Capabilities.__dataclass_fields__},
                             "observed_at": NOW, "approvals": ApprovalLevel.NEVER_ASK,
                             "approval_mode": "bypassPermissions"})  # fmt: skip
    html = page(FULL, bypass)
    assert '<span class="badge error">runs anything without asking</span>' in html
    assert "Recent changes" in html and "approvals changed: ask → never_ask" in html


def test_untrusted_values_are_escaped() -> None:
    evil = Capabilities("a1", NOW, mcp_servers=(McpServer("<script>alert(1)</script>", "stdio",
                                                          "npx"),))  # fmt: skip
    html = page(evil)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_channels_and_who_can_reach_it() -> None:
    html = page(
        Capabilities(
            "a1",
            NOW,
            channels=(
                Channel("telegram", "allowlist", False, 2, True),
                Channel("discord", "open", True),
            ),
        )
    )
    assert "<h3>Messaging channels</h3>" in html
    assert "Who can message it: <strong>anyone (direct messages are open)</strong>" in html
    assert '<span class="badge error">open to anyone</span>' in html
    assert "<code>allowlist</code>" in html and '<span class="badge degraded">yes</span>' in html
    assert "None configured." in page(Capabilities("a1", NOW))


def test_unknown_channels_are_shown_as_unknown() -> None:
    html = page(
        Capabilities("a1", NOW - timedelta(minutes=5)), Capabilities("a1", NOW, channels=None)
    )
    assert "Who can message it: <strong>unknown</strong>" in html
    assert "the messaging channels it answers on couldn't be read" in html
