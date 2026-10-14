import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.charts import Series, assign_slots, hbars, money, stacked_bars
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, UsageQuality, UsageRecord

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")


def client_with(records: list[UsageRecord], agents: int = 2) -> TestClient:
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, NOW)
    inv.upsert_agents(
        [AgentInstance(f"a{i}", "h-pi", "claude-code", f"Agent {i}") for i in range(agents)], NOW
    )
    UsageStore(db).add_records(records, "h-pi")
    app = create_app(HubSettings(display_timezone="UTC"), db)
    return TestClient(app, base_url="http://127.0.0.1:7878")


def rec(rid: str, agent: str = "a0", days_ago: int = 0, inp: int | None = 1_000_000,
        model: str = "claude-opus-5-5") -> UsageRecord:  # fmt: skip
    quality = UsageQuality.EXACT if inp is not None else UsageQuality.UNKNOWN
    return UsageRecord(rid, agent, NOW - timedelta(days=days_ago), session_id="sess-" + rid,
                       provider="anthropic", response_model=model, input_tokens=inp,
                       output_tokens=0 if inp is not None else None, quality=quality)  # fmt: skip


def test_empty_usage_page() -> None:
    html = client_with([]).get("/usage").text
    assert "No usage recorded in this period." in html
    assert "list-price equivalent" in html


def test_usage_page_with_data() -> None:
    html = client_with([rec("1"), rec("2", "a1", days_ago=1)]).get("/usage?days=7").text
    assert '<svg class="chart"' in html and 'aria-label="Daily cost by agent"' in html
    assert "<title>Agent 0 · pi5 · " in html  # per-mark tooltip
    assert '<span class="swatch s1"></span>Agent 0 · pi5' in html  # legend
    assert "$8.00" in html  # total: two calls at $4
    assert "Show as table" in html
    assert 'href="/agents/a0"' in html


def test_incomplete_cost_is_marked_lower_bound() -> None:
    html = client_with([rec("1"), rec("2", inp=None)]).get("/usage").text
    assert "≥ $4.00" in html
    assert "lower bounds" in html
    assert "1 unknown" in html


def test_custom_range_and_validation() -> None:
    client = client_with([rec("1", days_ago=40)])
    first = (NOW - timedelta(days=45)).date().isoformat()
    last = (NOW - timedelta(days=35)).date().isoformat()
    assert "$4.00" in client.get(f"/usage?from={first}&to={last}").text
    assert "No usage recorded" in client.get("/usage?days=7").text
    assert client.get("/usage?days=9999").status_code == 422


def test_colors_follow_entities_not_rank() -> None:
    assert assign_slots(["b", "a", "c"]) == {"a": 1, "b": 2, "c": 3}
    assert assign_slots(["c", "a"]) == {"a": 1, "c": 2}
    assert len(assign_slots([str(i) for i in range(12)])) == 8


def test_more_than_eight_agents_fold_into_other() -> None:
    records = [rec(str(i), f"a{i}") for i in range(10)]
    html = client_with(records, agents=10).get("/usage").text
    assert '<span class="swatch s-other"></span>Other' in html


def test_chart_escapes_labels() -> None:
    evil = "<script>x</script>"
    svg = stacked_bars(["2026-10-14"], [Series("a", evil, 1, {"2026-10-14": Decimal(1)})])
    assert evil not in svg and "&lt;script&gt;" in svg
    assert evil not in hbars([(evil, 10.0)])


def test_money_format() -> None:
    assert money(Decimal(0)) == "$0"
    assert money(Decimal("0.004")) == "<$0.01"
    assert money(Decimal("1234.5")) == "$1,234.50"


def test_page_is_fast_with_90_days() -> None:
    records = [rec(str(i), f"a{i % 2}", days_ago=i % 90) for i in range(5000)]
    client = client_with(records)
    start = time.perf_counter()
    assert client.get("/usage?days=90").status_code == 200
    assert time.perf_counter() - start < 2
