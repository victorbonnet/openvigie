from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.ui import host_views
from openvigie.model import AdapterHealth, AgentInstance, AgentStatus, HealthState, Host

NOW = datetime.now(UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.2")


def setup() -> tuple[TestClient, Inventory]:
    inv = Inventory(Database(MEMORY))
    inv.upsert_host(PI, NOW, clock_skew_s=0.4)
    inv.upsert_host(MAC, NOW - timedelta(minutes=10), clock_skew_s=-300.0)
    inv.upsert_agents(
        [AgentInstance("a1", "h-pi", "codex", "Codex", status=AgentStatus.ACTIVE),
         AgentInstance("a2", "h-pi", "claude-code", "Claude Code", status=AgentStatus.IDLE),
         AgentInstance("a3", "h-mac", "codex", "Codex", status=AgentStatus.ACTIVE)],
        NOW,
    )  # fmt: skip
    inv.set_adapter_health(
        "h-pi",
        [AdapterHealth("codex", HealthState.DEGRADED, "<b>bad</b> toml", parse_errors=3),
         AdapterHealth("claude-code", HealthState.OK)],
        NOW,
    )  # fmt: skip
    return TestClient(create_app(HubSettings(), inv.db), base_url="http://127.0.0.1:7878"), inv


def test_host_views_flags() -> None:
    _, inv = setup()
    views = {v.row.host.host_id: v for v in host_views(inv, NOW)}
    assert not views["h-pi"].stale and not views["h-pi"].skewed
    assert views["h-pi"].counts == {"active": 1, "idle": 1}
    assert views["h-pi"].unhealthy == 1
    assert views["h-mac"].stale and views["h-mac"].skewed


def test_hosts_page() -> None:
    client, _ = setup()
    html = client.get("/hosts").text
    assert 'href="/hosts/h-pi"' in html and "linux/aarch64" in html
    assert '<span class="badge stale">stale</span>' in html
    assert "clock skew" in html
    assert "1 need attention" in html


def test_host_page_lists_agents_and_adapter_health_escaped() -> None:
    client, _ = setup()
    html = client.get("/hosts/h-pi").text
    assert "<h1>pi5</h1>" in html
    assert 'hx-get="/agents/rows?host=h-pi"' in html
    assert "Codex" in html and "Claude Code" in html
    assert "&lt;b&gt;bad&lt;/b&gt; toml" in html and "<b>bad</b>" not in html
    assert '<td class="num">3</td>' in html


def test_nav_badge_counts_unhealthy_adapters() -> None:
    client, _ = setup()
    assert 'title="adapters needing attention">1</span>' in client.get("/").text


def test_unknown_host_404() -> None:
    client, _ = setup()
    assert client.get("/hosts/nope").status_code == 404
