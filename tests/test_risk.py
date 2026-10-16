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
    Channel,
    FsAccess,
    Hook,
    Host,
    McpServer,
    NetAccess,
    SandboxState,
    SecretRef,
)
from openvigie.risk import evaluate, facts, level, load_rules, reachable_by_others, reached_by

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
AGENT = AgentInstance("a1", "h", "claude-code", "Claude Code", sandboxed=SandboxState.NO)
CALM = Capabilities("a1", NOW, ApprovalLevel.ASK, "default", FsAccess.WORKSPACE,
                    network=NetAccess.NONE, shell=True)  # fmt: skip


OWNER_ONLY = Channel("telegram", "allowlist", False, 1, False)
GROUPS = Channel("telegram", "allowlist", False, 1, True)


def caps(**changes: object) -> Capabilities:
    fields = {f: getattr(CALM, f) for f in Capabilities.__dataclass_fields__}
    return Capabilities(**{**fields, **changes})


@pytest.fixture
def rules(tmp_path: Path):  # type: ignore[no-untyped-def]
    return load_rules(tmp_path / "none.toml")


@pytest.mark.parametrize(
    ("capabilities", "agent", "host_os", "expected"),
    [
        (CALM, AGENT, "linux", []),
        (caps(approvals=ApprovalLevel.NEVER_ASK, approval_mode="bypassPermissions"), AGENT,
         "linux", ["never-asks-unsandboxed"]),
        (caps(approvals=ApprovalLevel.NEVER_ASK), AgentInstance(
            "a1", "h", "codex", "Codex", sandboxed=SandboxState.YES), "linux", []),
        (caps(network=NetAccess.FULL), AGENT, "linux", ["shell-network-unsandboxed"]),
        (caps(filesystem=FsAccess.FULL), AGENT, "linux", ["full-filesystem"]),
        (caps(mcp_servers=(McpServer("gh", "stdio", "npx", pinned=False),
                           McpServer("db", "stdio", "uvx", pinned=True))), AGENT, "linux",
         ["unpinned-mcp"]),
        (caps(secrets=(SecretRef("GITHUB_TOKEN", "env"),)), AGENT, "linux",
         ["secrets-reachable"]),
        (caps(hooks=(Hook("PreToolUse", "jq"),)), AGENT, "linux", ["hooks-run-commands"]),
        (CALM, AGENT, "darwin", ["macos-no-sandbox"]),
        (caps(channels=(OWNER_ONLY,)), AGENT, "linux", []),
        (caps(channels=(OWNER_ONLY, Channel("discord", "open", True))), AGENT, "linux",
         ["others-instruct"]),
        (caps(channels=(GROUPS,), network=NetAccess.FULL), AGENT, "linux",
         ["others-instruct-broad-access", "shell-network-unsandboxed", "others-instruct"]),
        (caps(channels=(GROUPS,), filesystem=FsAccess.FULL), AgentInstance(
            "a1", "h", "hermes", "Hermes", sandboxed=SandboxState.YES), "linux",
         ["others-instruct"]),
        (caps(channels=(Channel("telegram", "pairing"),)), AGENT, "linux", []),
    ],
)  # fmt: skip
def test_each_rule(rules, capabilities: Capabilities, agent: AgentInstance,  # type: ignore[no-untyped-def]
                   host_os: str, expected: list[str]) -> None:  # fmt: skip
    assert [f.rule_id for f in evaluate(rules, facts(agent, capabilities, host_os))] == expected


def test_reason_quotes_facts_and_level(rules) -> None:  # type: ignore[no-untyped-def]
    found = evaluate(rules, facts(AGENT, caps(
        approvals=ApprovalLevel.NEVER_ASK, approval_mode="bypassPermissions",
        mcp_servers=(McpServer("gh", "stdio", "npx", pinned=False),))))  # fmt: skip
    assert [f.severity.label for f in found] == ["high", "medium"]  # most severe first
    assert "(bypassPermissions)" in found[0].reason and found[1].reason.startswith("gh run")
    assert found[0].facts == {"approvals": "never_ask", "sandboxed": "no"}
    assert level(found, True) == "high"
    assert level([], True) == "none" and level([], False) == "unknown"
    assert evaluate(rules, facts(AGENT, None)) == []  # nothing to assess: "unknown", no flags


def test_override_adds_replaces_and_disables(tmp_path: Path) -> None:
    path = tmp_path / "risk.toml"
    path.write_text("""
[[rules]]
id = "hooks-run-commands"
enabled = false

[[rules]]
id = "unpinned-mcp"
severity = "high"
title = "Unpinned MCP"
reason = "pin {mcp_unpinned}"
when = { field = "mcp_unpinned", nonempty = true }

[[rules]]
id = "codex-anywhere"
severity = "low"
title = "Codex"
reason = "Codex is here"
when = { any = [
  { field = "agent_type", equals = "codex" },
  { not = { field = "shell", in = [true] } },
] }
""")
    rules = load_rules(path)
    assert "hooks-run-commands" not in rules.rules and rules.problems == []
    assert rules.rules["unpinned-mcp"].severity.label == "high"
    found = evaluate(rules, facts(AgentInstance("a1", "h", "codex", "Codex"), CALM))
    assert [f.rule_id for f in found] == ["codex-anywhere"]


RULE = '[[rules]]\nid = "{id}"\nseverity = "{severity}"\ntitle = "t"\nreason = "r"\nwhen = {when}'


@pytest.mark.parametrize(
    "bad",
    [
        "not toml [",
        RULE.format(id="x", severity="huge", when='{ field = "a", equals = 1 }'),
        RULE.format(id="x", severity="low", when='{ field = "a", eval = "1" }'),
        RULE.format(id="Bad Id", severity="low", when="{}"),
    ],
)
def test_broken_override_is_reported_and_bundled_rules_stay(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "risk.toml"
    path.write_text(bad)
    rules = load_rules(path)
    assert rules.problems and str(path) in rules.problems[0]
    assert "never-asks-unsandboxed" in rules.rules and len(rules.rules) == 9


def test_agents_api_exposes_risk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(Host("h", "mbp", "darwin", "arm64", "0.1.0"), NOW)
    inv.upsert_agents([AGENT, AgentInstance("a2", "h", "codex", "Codex")], NOW)
    CapabilityStore(db).put([caps(approvals=ApprovalLevel.NEVER_ASK)], "h")
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    body = client.get("/api/v1/agents").json()
    by_id = {a["agent"]["agent_id"]: a for a in body["agents"]}
    assert by_id["a1"]["risk"]["level"] == "high"
    assert [f["id"] for f in by_id["a1"]["risk"]["flags"]] == ["never-asks-unsandboxed",
                                                                "macos-no-sandbox"]  # fmt: skip
    assert by_id["a2"]["risk"]["level"] == "low"  # no capabilities, but on macOS
    assert body["rule_problems"] == []


@pytest.mark.parametrize(
    ("channels", "others", "who"),
    [
        ((), False, "nobody: no messaging channel"),
        ((OWNER_ONLY,), False, "1 allowlisted person"),
        ((OWNER_ONLY, Channel("slack", "allowlist", False, 2, False)), True,
         "3 allowlisted people"),
        ((GROUPS,), True, "members of its group chats"),
        ((OWNER_ONLY, Channel("discord", "open", True)), True, "anyone: direct messages are open"),
        ((Channel("telegram", "pairing"),), None, "unknown"),
        ((Channel("telegram", groups=True),), True, "members of its group chats"),
    ],
)  # fmt: skip
def test_who_can_reach_an_agent(
    channels: tuple[Channel, ...], others: bool | None, who: str
) -> None:
    assert reachable_by_others(channels) is others
    assert reached_by(channels) == who


def test_unknown_reach_is_a_fact_not_a_flag(rules) -> None:  # type: ignore[no-untyped-def]
    f = facts(AGENT, caps(channels=(Channel("telegram", "pairing"),)), "linux")
    assert f["reachable_by_others"] == "unknown"
    assert f["channels"] == ["telegram"]
    assert evaluate(rules, f) == []
