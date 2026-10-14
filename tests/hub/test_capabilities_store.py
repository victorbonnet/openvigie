from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openvigie.adapters.base import Adapter, HostContext
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST, fake_context
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.capabilities import CapabilityStore, diff
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import (
    SCHEMA_VERSION,
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    CodecError,
    FsAccess,
    Hook,
    McpServer,
    SecretRef,
    from_dict,
    to_dict,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
BASE = Capabilities(
    "a1", NOW, ApprovalLevel.ASK, "default", FsAccess.WORKSPACE,
    mcp_servers=(McpServer("github", "stdio", "npx", pinned=True),),
    skills=("pdf", "docx"), hooks=(Hook("PreToolUse", "jq"),),
    secrets=(SecretRef("GITHUB_TOKEN", "mcp:github"),),
)  # fmt: skip


def seeded() -> Database:
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(TEST_HOST, NOW)
    inv.upsert_agents([AgentInstance("a1", TEST_HOST.host_id, "claude-code", "Claude Code")], NOW)
    return db


def test_codec_round_trip_and_no_room_for_secret_values() -> None:
    assert from_dict(Capabilities, to_dict(BASE)) == BASE
    with pytest.raises(CodecError, match="unknown field"):
        from_dict(SecretRef, {"name": "GITHUB_TOKEN", "where": "env", "value": "ghp_x"})
    with pytest.raises(CodecError):
        from_dict(Capabilities, {**to_dict(BASE), "approvals": "sometimes"})


def test_diff_lists_every_change() -> None:
    changed = Capabilities(
        "a1", NOW, ApprovalLevel.NEVER_ASK, "bypassPermissions", FsAccess.WORKSPACE,
        mcp_servers=(McpServer("github", "stdio", "npx", pinned=False),
                     McpServer("fs", "stdio", "uvx", pinned=False)),
        skills=("pdf",), hooks=(Hook("PreToolUse", "jq"),),
        secrets=(SecretRef("GITHUB_TOKEN", "mcp:github"),),
    )  # fmt: skip
    assert set(diff(BASE, changed)) == {
        ("approvals", "changed", "ask → never_ask"),
        ("approval_mode", "changed", "default → bypassPermissions"),
        ("mcp_servers", "added", "fs stdio uvx (unpinned)"),
        ("mcp_servers", "changed", "github: stdio npx → stdio npx (unpinned)"),
        ("skills", "removed", "docx"),
    }
    assert diff(BASE, BASE) == []


def test_store_keeps_latest_and_logs_changes_only() -> None:
    db = seeded()
    store = CapabilityStore(db)
    assert store.put([BASE], TEST_HOST.host_id) == 1  # first snapshot: no change entries
    assert store.changes("a1") == []
    later = NOW + timedelta(hours=1)
    assert store.put([Capabilities(**{**_fields(BASE), "observed_at": later})],
                     TEST_HOST.host_id) == 0  # fmt: skip
    bypass = Capabilities(**{**_fields(BASE), "observed_at": later,
                             "approvals": ApprovalLevel.NEVER_ASK})  # fmt: skip
    assert store.put([bypass], TEST_HOST.host_id) == 1
    (change,) = store.changes("a1")
    assert (change.field, change.change, change.detail) == ("approvals", "changed",
                                                            "ask → never_ask")  # fmt: skip
    assert store.get("a1") == bypass
    assert store.put([BASE], "another-host") == 0  # can't write another host's agent


def _fields(caps: Capabilities) -> dict[str, Any]:
    return {name: getattr(caps, name) for name in Capabilities.__dataclass_fields__}


def test_ingest_accepts_capabilities() -> None:
    db = seeded()
    token = Credentials(db).issue_token(TEST_HOST.host_id, NOW)
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    body = {"schema_version": SCHEMA_VERSION, "host": to_dict(TEST_HOST),
            "sent_at": datetime.now(UTC).isoformat(), "capabilities": [to_dict(BASE)]}  # fmt: skip
    reply = client.post("/api/v1/ingest", json=body, headers={"Authorization": f"Bearer {token}"})
    assert reply.json()["accepted"]["capabilities"] == 1
    assert CapabilityStore(db).get("a1") == BASE


class Fake(Adapter):
    agent_type = "fake"
    display_name = "Fake"

    def __init__(self) -> None:
        super().__init__()
        self.caps = BASE

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        return [AgentInstance("a1", ctx.host.host_id, "fake", "Fake")]

    def capabilities(self, ctx: HostContext, agent: AgentInstance) -> Capabilities | None:
        return self.caps


def test_collector_sends_capabilities_when_they_change() -> None:
    sent: list[list[dict[str, Any]]] = []

    def hub(method: str, url: str, body: bytes | None, headers: Any,
            timeout: float) -> tuple[int, bytes]:  # fmt: skip
        import json

        caps = json.loads(body or b"{}").get("capabilities")
        if caps:
            sent.append(caps)
        return 200, b'{"ignored": []}'

    fake = Fake()
    client = HubClient("http://127.0.0.1:7878", token="t", transport=hub)  # noqa: S106
    c = Collector(CollectorSettings(slow_tick_s=0.0), fake_context(homes=[]),
                  Discovery(adapters=[fake]), client)  # fmt: skip
    c.tick()
    c.tick()
    assert len(sent) == 1  # unchanged: not re-sent
    fake.caps = Capabilities(**{**_fields(BASE), "skills": ("pdf",)})
    c.tick()
    assert len(sent) == 2 and sent[1][0]["skills"] == ["pdf"]
