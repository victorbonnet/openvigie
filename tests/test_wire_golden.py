"""The collector ↔ hub wire format must not change by accident (SCHEMA_VERSION 1).

One sample of every wire model is encoded and compared with a recorded golden file, and decoded
back to an equal object. Regenerate with OPENVIGIE_UPDATE_GOLDEN=1 only for an intended change.
"""

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    ApprovalLevel,
    Capabilities,
    FsAccess,
    HealthState,
    Hook,
    Host,
    McpServer,
    NetAccess,
    OpenShellSandbox,
    SandboxEndpoint,
    SandboxRule,
    SandboxState,
    SecretRef,
    Session,
    Span,
    SpanStatus,
    StatusEvent,
    UsageBackfill,
    UsageQuality,
    UsageRecord,
    from_dict,
    to_dict,
)

GOLDEN = Path(__file__).parent / "fixtures" / "wire_golden.json"
AT = datetime(2026, 10, 14, 12, 30, 15, 123456, tzinfo=UTC)
PARIS = timezone(timedelta(hours=2))

SAMPLES: dict[str, Any] = {
    "Host": Host("h1", "pi5", "linux", "aarch64", "0.1.0"),
    "AgentInstance": AgentInstance(
        "a1", "h1", "claude-code", "Claude Code", version="2.1.299", config_root="~/.claude",
        sandboxed=SandboxState.NO, sandbox_kind=None, process_running=True,
        last_activity_at=AT, status=AgentStatus.ACTIVE,
    ),
    "StatusEvent": StatusEvent("a1", AgentStatus.IDLE, AgentStatus.ACTIVE, AT),
    "Session": Session("s1", "a1", AT, AT.astimezone(PARIS), ("claude-opus-5-5",)),
    "UsageRecord": UsageRecord(
        "r1", "a1", AT, session_id="s1", provider="anthropic", request_model=None,
        response_model="claude-opus-5-5", input_tokens=12, output_tokens=None,
        cache_read_input_tokens=0, cache_creation_input_tokens=5,
        cache_creation_1h_input_tokens=None, reasoning_tokens=3, quality=UsageQuality.EXACT,
    ),
    "Span": Span("ab" * 16, "cd" * 8, "chat claude-opus-5-5", AT, None, "ef" * 8, "a1",
                 SpanStatus.ERROR, {"gen_ai.usage.input_tokens": 12, "session.id": "s1",
                                    "flag": True, "ratio": 0.5}),
    "UsageBackfill": UsageBackfill("a1", AT, False, AT - timedelta(days=7), 1_200_000_000),
    "AdapterHealth": AdapterHealth("codex", HealthState.DEGRADED, "bad line", 2, 1),
    "Capabilities": Capabilities(
        "a1", AT, ApprovalLevel.NEVER_ASK, "bypassPermissions", FsAccess.WORKSPACE, ("~/x",),
        NetAccess.ALLOWLIST, ("github.com",), True, ("Bash(ls)",), ("WebFetch",),
        (McpServer("gh", "stdio", "npx", None, False, ("GH_TOKEN",)),
         McpServer("docs", "http", None, "mcp.example.com", None)),
        ("pdf",), (Hook("PreToolUse", "jq"),), (SecretRef("GH_TOKEN", "mcp:gh"),),
        ("~/.claude/settings.json",),
    ),
    "OpenShellSandbox": OpenShellSandbox(
        "267c2951", "h1", "ov-net", AT, "default", "Ready", {"openvigie.agent": "claude-code"},
        "claude-code", ("a1",), 3, "4b27e5bf",
        (SandboxRule("github_readonly", (SandboxEndpoint("api.github.com", (443,), None, "rest",
                                                         "read-only", "enforce"),),
                     ("/usr/bin/curl",)),),
        '{"github_readonly":{"binaries":[{"path":"/usr/bin/curl"}],"endpoints":[{"host":'
        '"api.github.com","port":443}],"name":"github_readonly"}}',
    ),
}  # fmt: skip


def test_wire_format_is_unchanged() -> None:
    encoded = {name: to_dict(obj) for name, obj in SAMPLES.items()}
    if os.environ.get("OPENVIGIE_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(encoded, indent=1, sort_keys=True) + "\n")
    assert json.loads(json.dumps(encoded)) == json.loads(GOLDEN.read_text())
    for name, obj in SAMPLES.items():
        assert from_dict(type(obj), json.loads(json.dumps(encoded[name]))) == obj, name
