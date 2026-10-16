import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.reports import usage_report
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, UsageQuality, UsageRecord
from openvigie.pricing import PriceTable, load_price_table

PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
DAY = datetime(2026, 10, 14, tzinfo=UTC)


@pytest.fixture
def prices(tmp_path: Path) -> PriceTable:
    return load_price_table(tmp_path / "none.toml")


def seed(db: Database, records: list[UsageRecord]) -> None:
    inv = Inventory(db)
    inv.upsert_host(PI, DAY)
    inv.upsert_agents([AgentInstance("cc", "h-pi", "claude-code", "Claude Code"),
                       AgentInstance("cx", "h-pi", "codex", "Codex")], DAY)  # fmt: skip
    UsageStore(db).add_records(records, "h-pi")


def rec(rid: str, agent: str, when: datetime, model: str, inp: int | None, out: int | None,
        provider: str = "anthropic", **kw: Any) -> UsageRecord:  # fmt: skip
    quality = kw.pop("quality", UsageQuality.EXACT if inp is not None else UsageQuality.UNKNOWN)
    return UsageRecord(rid, agent, when, session_id=kw.pop("session", "s1"), provider=provider,
                       response_model=model, input_tokens=inp, output_tokens=out,
                       quality=quality, **kw)  # fmt: skip


def test_totals_by_agent_model_day(prices: PriceTable) -> None:
    db = Database(MEMORY)
    seed(db, [
        rec("1", "cc", DAY + timedelta(hours=1), "claude-opus-5-5", 1_000_000, 0),
        rec("2", "cc", DAY + timedelta(hours=2), "claude-opus-5-5", 0, 1_000_000),
        rec("3", "cc", DAY + timedelta(days=1), "claude-sonnet-5-5", 1_000_000, 0),
        rec("4", "cx", DAY + timedelta(hours=3), "gpt-5", 1_000_000, 0, provider="openai"),
    ])  # fmt: skip
    report = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=2),
                          group_by=["agent", "day"])  # fmt: skip
    by_key = {(r.key["agent"], r.key["day"]): r for r in report.rows}
    assert by_key[("cc", "2026-10-14")].cost == Decimal(24)  # 4 + 20
    assert by_key[("cc", "2026-10-15")].cost == Decimal(2)
    assert by_key[("cx", "2026-10-14")].cost == Decimal("1.25")
    assert report.total.cost == Decimal("27.25") and report.total.cost_complete
    assert by_key[("cc", "2026-10-14")].labels["agent"] == "Claude Code · pi5"


def test_unknown_and_unpriced_are_never_zero(prices: PriceTable) -> None:
    db = Database(MEMORY)
    seed(db, [
        rec("1", "cc", DAY, "claude-opus-5-5", 1_000_000, 0),
        rec("2", "cc", DAY, "claude-opus-5-5", None, None),
        rec("3", "cc", DAY, "mystery-model", 500, 500),
    ])  # fmt: skip
    total = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1)).total
    assert total.cost == Decimal(4)
    assert total.unknown == 1 and total.unpriced_tokens == 1000
    assert total.cost_complete is False
    assert total.input_tokens == 1_000_500  # the unknown record adds nothing, not zero-filled


def test_days_bucket_in_display_timezone(prices: PriceTable) -> None:
    db = Database(MEMORY)
    late = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)  # already Oct 15 in Zurich
    seed(db, [rec("1", "cc", late, "claude-opus-5-5", 1, 1)])
    zurich = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=2),
                          group_by=["day"], tz=ZoneInfo("Europe/Zurich"))  # fmt: skip
    utc = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=2), group_by=["day"])
    assert [r.key["day"] for r in zurich.rows] == ["2026-10-15"]
    assert [r.key["day"] for r in utc.rows] == ["2026-10-14"]


def test_haiku_tier_is_per_request_not_per_group(prices: PriceTable) -> None:
    db = Database(MEMORY)
    small = [rec(f"s{i}", "cc", DAY, "claude-haiku-5-5", 60_000, 0) for i in range(3)]
    seed(db, small)
    total = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1)).total
    assert total.cost == Decimal(180_000) * Decimal("0.10") / Decimal(1_000_000)


def test_usage_api(prices: PriceTable) -> None:
    db = Database(MEMORY)
    seed(db, [rec("1", "cc", datetime.now(UTC), "claude-opus-5-5", 1_000_000, 0)])
    client = TestClient(create_app(HubSettings(display_timezone="UTC"), db),
                        base_url="http://127.0.0.1:7878")  # fmt: skip
    data = client.get("/api/v1/usage?group_by=model").json()
    assert data["rows"][0]["key"] == {"model": "claude-opus-5-5"}
    assert data["total"]["cost_usd"] == "4.000000"
    assert data["total"]["cost_complete"] is True
    assert client.get("/api/v1/usage?group_by=planet").status_code == 422
    assert client.get("/api/v1/usage?from=2026-10-10&to=2026-10-01").status_code == 422
    assert client.get("/api/v1/usage?from=yesterday").status_code == 422


def test_large_range_is_fast(prices: PriceTable) -> None:
    db = Database(MEMORY)
    records = [
        rec(str(i), "cc" if i % 2 else "cx", DAY - timedelta(minutes=i), "claude-opus-5-5",
            1000, 100, session=f"s{i % 50}")
        for i in range(200_000)
    ]  # fmt: skip
    seed(db, records)
    start = time.perf_counter()
    report = usage_report(db, prices, start=DAY - timedelta(days=200),
                          end=DAY + timedelta(minutes=1),
                          group_by=["agent", "model", "day"])  # fmt: skip
    elapsed = time.perf_counter() - start
    print(f"aggregated 200k records in {elapsed:.2f}s")
    assert report.total.records == 200_000
    assert elapsed < 3, f"{elapsed:.2f}s"


def test_group_by_session(prices: PriceTable) -> None:
    db = Database(MEMORY)
    seed(db, [rec("1", "cc", DAY, "claude-opus-5-5", 1, 1, session="a"),
              rec("2", "cc", DAY, "claude-opus-5-5", 1, 1, session="b"),
              rec("3", "cc", DAY, "claude-opus-5-5", 1, 1, session="b")])  # fmt: skip
    report = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1), group_by=["session"])
    assert {r.key["session"]: r.records for r in report.rows} == {"a": 1, "b": 2}
    assert len(usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1)).rows) == 1


def test_openai_tier_counts_cached_input_once(prices: PriceTable) -> None:
    # 150K input of which 140K cached: under gpt-6's 272K threshold even though input + cached
    # would exceed it, and above Haiku's 100K threshold, which must not switch gpt-6's tier.
    db = Database(MEMORY)
    seed(db, [rec("1", "cx", DAY, "gpt-6.1-sol", 150_000, 0, provider="openai",
                  cache_read_input_tokens=140_000),
              rec("2", "cx", DAY, "gpt-6.1-sol", 300_000, 0, provider="openai")])  # fmt: skip
    total = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1)).total
    short = Decimal(10_000) * 2 + Decimal(140_000) * Decimal("0.10")
    long = Decimal(300_000) * 4
    assert total.cost == (short + long) / Decimal(1_000_000)


# --- history still being read (FR-3) -----------------------------------------------------------


def reading(db: Database, agent: str, until: datetime | None, at: datetime = DAY,
            complete: bool = False) -> int:  # fmt: skip
    from openvigie.model import UsageBackfill

    return UsageStore(db).set_backfill(
        [UsageBackfill(agent, at, complete, None if complete else until, 0 if complete else 7)],
        "h-pi",
    )


def test_every_row_of_an_agent_reading_history_is_incomplete(prices: PriceTable) -> None:
    db = Database(MEMORY)
    seed(
        db,
        [
            rec("1", "cc", DAY, "claude-opus-5-5", 1_000_000, 0),
            rec("2", "cx", DAY, "gpt-5-codex", 1_000_000, 0, provider="openai"),
            rec("3", "cx", DAY + timedelta(days=2), "gpt-5-codex", 1, 0, provider="openai"),
        ],
    )
    # files last written before this are read; a resumed session's file written later can
    # still hold older records, so it says nothing about which days are complete
    reading(db, "cx", DAY + timedelta(days=1, hours=6))  # fmt: skip
    start, end = DAY - timedelta(days=1), DAY + timedelta(days=3)

    by_agent = usage_report(db, prices, group_by=["agent"], start=start, end=end)
    assert [(r.key["agent"], r.history_complete) for r in by_agent.rows] == [
        ("cc", True), ("cx", False)]  # fmt: skip
    assert by_agent.total.history_complete is False and by_agent.total.cost_complete is False
    assert [(r.agent_id, r.label) for r in by_agent.reading] == [("cx", "Codex · pi5")]

    by_day = usage_report(db, prices, group_by=["agent", "day"], start=start, end=end)
    assert {(r.key["agent"], r.key["day"]): r.history_complete for r in by_day.rows} == {
        ("cc", "2026-10-14"): True,
        ("cx", "2026-10-14"): False,  # before read_until, but may still be missing records
        ("cx", "2026-10-16"): False,
    }
    assert usage_report(db, prices, agent_id="cc", start=start, end=end).reading == []

    before = usage_report(db, prices, start=DAY, end=DAY + timedelta(days=1))
    assert [r.agent_id for r in before.reading] == ["cx"] and not before.total.cost_complete
    by_model = usage_report(db, prices, group_by=["model"], start=start, end=end)
    assert {r.key["model"]: r.history_complete for r in by_model.rows} == {
        "claude-opus-5-5": False, "gpt-5-codex": False}  # fmt: skip


def test_backfill_keeps_the_newest_report() -> None:
    db = Database(MEMORY)
    seed(db, [])
    assert reading(db, "cx", DAY, at=DAY + timedelta(minutes=1)) == 1
    assert reading(db, "cx", None, at=DAY, complete=True) == 0  # older, e.g. from the spool
    state = UsageStore(db).backfill("cx")
    assert state is not None and not state.complete and state.read_until == DAY
    assert reading(db, "cx", None, at=DAY + timedelta(minutes=2), complete=True) == 1
    state = UsageStore(db).backfill("cx")
    assert state is not None and state.complete and state.read_until is None
    assert UsageStore(db).set_backfill([], "h-pi") == 0
    assert reading(db, "unknown-agent", DAY) == 0  # not this host's agent
