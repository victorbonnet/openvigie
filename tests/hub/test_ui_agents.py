from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.ui import ago
from openvigie.model import AgentInstance, AgentStatus, Host, SandboxState

NOW = datetime.now(UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.1")


@pytest.fixture
def inv() -> Inventory:
    return Inventory(Database(MEMORY))


def client_for(inv: Inventory) -> TestClient:
    return TestClient(create_app(HubSettings(), inv.db), base_url="http://127.0.0.1:7878")


def seed(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.upsert_host(MAC, NOW)
    inv.upsert_agents(
        [
            AgentInstance("a1", "h-pi", "claude-code", "Claude Code", version="2.1.0",
                          config_root="~/.claude", sandboxed=SandboxState.YES,
                          sandbox_kind="bwrap", status=AgentStatus.ACTIVE,
                          last_activity_at=NOW - timedelta(minutes=2)),
            AgentInstance("a2", "h-mac", "codex", "Codex", status=AgentStatus.UNKNOWN),
        ],
        NOW,
    )  # fmt: skip


def test_empty_state_explains_next_steps(inv: Inventory) -> None:
    html = client_for(inv).get("/").text
    assert "No agents yet" in html
    assert "openvigie hub enrol-code" in html


def test_lists_agents_with_text_statuses(inv: Inventory) -> None:
    seed(inv)
    html = client_for(inv).get("/").text
    assert 'href="/agents/a1"' in html and "Claude Code" in html
    assert '<span class="badge active">active</span>' in html
    assert '<span class="badge unknown">unknown</span>' in html  # sandbox unknown != no
    assert "yes · bwrap" in html
    assert "2m ago" in html
    assert 'hx-trigger="every 5s"' in html


def test_filters_keep_polling_query(inv: Inventory) -> None:
    seed(inv)
    html = client_for(inv).get("/?type=codex").text
    assert "Codex" in html and "Claude Code</a>" not in html
    assert 'hx-get="/agents/rows?type=codex"' in html
    assert '<option value="codex" selected>' in html


def test_rows_partial_returns_only_tbody(inv: Inventory) -> None:
    seed(inv)
    html = client_for(inv).get("/agents/rows?status=active").text.strip()
    assert html.startswith('<tbody id="agent-rows"') and html.endswith("</tbody>")
    assert "a1" in html and "a2" not in html
    assert "<html" not in html


def test_no_match_message(inv: Inventory) -> None:
    seed(inv)
    assert "No agents match" in client_for(inv).get("/?status=idle").text


def test_agent_name_is_escaped(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    evil = "<script>alert(1)</script>"
    inv.upsert_agents([AgentInstance("x", "h-pi", "codex", evil, version=evil)], NOW)
    html = client_for(inv).get("/").text
    assert evil not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_overlong_filter_rejected(inv: Inventory) -> None:
    assert client_for(inv).get("/?type=" + "x" * 500).status_code == 422


def test_ago() -> None:
    assert ago(None) == "unknown"
    assert ago(NOW - timedelta(hours=3), NOW) == "3h ago"
    assert ago(NOW + timedelta(seconds=30), NOW) == "just now"
