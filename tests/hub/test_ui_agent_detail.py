from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    HealthState,
    Host,
    StatusEvent,
)

NOW = datetime.now(UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")


def setup() -> tuple[TestClient, Inventory]:
    inv = Inventory(Database(MEMORY))
    inv.upsert_host(PI, NOW)
    inv.upsert_agents(
        [AgentInstance("a1", "h-pi", "codex", "Codex", version="0.46.0", config_root="~/.codex",
                       process_running=True, status=AgentStatus.ACTIVE,
                       last_activity_at=NOW - timedelta(seconds=30)),
         AgentInstance("a2", "h-pi", "claude-code", "Claude Code")],
        NOW,
    )  # fmt: skip
    inv.add_status_events(
        [StatusEvent("a1", AgentStatus.UNKNOWN, AgentStatus.IDLE, NOW - timedelta(hours=2)),
         StatusEvent("a1", AgentStatus.IDLE, AgentStatus.ACTIVE, NOW - timedelta(minutes=1))]
    )  # fmt: skip
    inv.set_adapter_health("h-pi", [AdapterHealth("codex", HealthState.DEGRADED, "bad toml")], NOW)
    client = TestClient(create_app(HubSettings(), inv.db), base_url="http://127.0.0.1:7878")
    return client, inv


def test_detail_page() -> None:
    client, _ = setup()
    html = client.get("/agents/a1").text
    assert "<h1>Codex</h1>" in html
    assert "0.46.0" in html and "<code>~/.codex</code>" in html
    assert "process running" in html
    assert '<span class="badge degraded">degraded</span>' in html and "bad toml" in html
    assert html.index("1m ago") < html.index("2h ago")  # newest first
    assert 'hx-get="/agents/a1/status"' in html


def test_unknowns_rendered_as_unknown() -> None:
    client, _ = setup()
    html = client.get("/agents/a2").text
    assert "Version</dt><dd>unknown" in html
    assert "process unknown" in html
    assert "No status changes recorded yet." in html
    assert 'Adapter</dt><dd>\n    <span class="badge unknown">' in html


def test_status_partial() -> None:
    client, _ = setup()
    html = client.get("/agents/a1/status").text.strip()
    assert html.startswith('<div id="agent-status"') and "<html" not in html


def test_unknown_agent_is_html_404() -> None:
    client, _ = setup()
    response = client.get("/agents/nope")
    assert response.status_code == 404
    assert "404 · Not found" in response.text
    assert "content-security-policy" in response.headers


def test_bad_agent_id_rejected() -> None:
    client, _ = setup()
    assert client.get("/agents/" + "x" * 200).status_code in (404, 422)
    assert client.get("/agents/%3Cscript%3E").status_code in (404, 422)


def test_api_errors_stay_json() -> None:
    client, _ = setup()
    response = client.post("/api/v1/ingest", json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid or revoked token"}
