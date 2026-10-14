"""Cost and token budgets with alerts (FR-14, D17)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.budgets import Budget, check, evaluate, load_budgets, period_start
from openvigie.hub.store.alerts import AlertStore
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import AgentInstance, Host, UsageQuality, UsageRecord
from openvigie.pricing import PriceTable, load_price_table

PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture
def prices(tmp_path: Path) -> PriceTable:
    return load_price_table(tmp_path / "none.toml")


def rec(rid: str, agent: str, when: datetime, inp: int | None, out: int | None = 0,
        model: str = "claude-opus-5-5") -> UsageRecord:  # fmt: skip
    quality = UsageQuality.EXACT if inp is not None else UsageQuality.UNKNOWN
    return UsageRecord(rid, agent, when, session_id="s1", provider="anthropic",
                       response_model=model, input_tokens=inp, output_tokens=out,
                       quality=quality)  # fmt: skip


@pytest.fixture
def db() -> Database:
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    Inventory(db).upsert_agents([AgentInstance("cc", "h-pi", "claude-code", "Claude Code"),
                                 AgentInstance("cx", "h-pi", "codex", "Codex")], NOW)  # fmt: skip
    UsageStore(db).add_records([
        rec("1", "cc", NOW - timedelta(hours=1), 1_000_000),       # $4 today
        rec("2", "cx", NOW - timedelta(hours=2), 200_000),         # codex today
        rec("3", "cc", NOW - timedelta(days=3), 1_000_000),        # $4 earlier this month
    ], "h-pi")  # fmt: skip
    return db


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "budgets.toml"
    path.write_text(text)
    return path


def test_loading(tmp_path: Path) -> None:
    assert load_budgets(tmp_path / "missing.toml").budgets == []
    ok = load_budgets(
        write(
            tmp_path,
            '[[budgets]]\nid = "a"\nmax_cost_usd = 5\n'
            '[[budgets]]\nid = "b"\nperiod = "month"\n'
            'host = "pi5"\nmax_tokens = 100\nwarn_at = 0.5\n',
        )
    )
    assert [b.id for b in ok.budgets] == ["a", "b"] and ok.problems == []
    assert ok.budgets[0].max_cost_usd == Decimal(5) and ok.budgets[1].scope == "host pi5"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ('[[budgets]]\nid = "a"\n', "set max_cost_usd, max_tokens or both"),
        ('[[budgets]]\nid = "A b"\nmax_tokens = 1\n', "budgets.budgets.0.id"),
        ('[[budgets]]\nid = "a"\nmax_cost_usd = true\n', "expected a number"),
        ('[[budgets]]\nid = "a"\nmax_tokens = 1\nwarn_at = 1.5\n', "warn_at"),
        ('[[budgets]]\nid = "a"\nmax_tokens = 1\nevil = 1\n', "unknown key"),
        ('[[budgets]]\nid = "a"\nmax_tokens = 1\n[[budgets]]\nid = "a"\nmax_tokens = 2\n',
         "same id"),
        ("not toml", "ignored"),
    ],
)  # fmt: skip
def test_broken_files_are_reported_and_ignored(tmp_path: Path, text: str, problem: str) -> None:
    loaded = load_budgets(write(tmp_path, text))
    assert loaded.budgets == [] and problem in loaded.problems[0]


def test_periods_start_in_the_display_time_zone() -> None:
    late = datetime(2026, 10, 14, 22, 30, tzinfo=UTC)  # already the 15th in Paris
    assert period_start("day", late, PARIS) == datetime(2026, 10, 15, tzinfo=PARIS)
    assert period_start("month", late, PARIS) == datetime(2026, 10, 1, tzinfo=PARIS)
    assert period_start("day", late, UTC) == datetime(2026, 10, 14, tzinfo=UTC)


def test_evaluation_and_levels(db: Database, prices: PriceTable) -> None:
    budgets = [
        Budget(id="cc-day", agent_type="claude-code", max_cost_usd=Decimal(5)),
        Budget(id="cc-month", agent_type="claude-code", period="month", max_cost_usd=Decimal(6)),
        Budget(id="all-tokens", max_tokens=10_000_000),
        Budget(id="codex", agent_id="cx", max_tokens=200_000),
    ]
    statuses = {s.budget.id: s for s in evaluate(db, prices, budgets, UTC, NOW)}
    assert statuses["cc-day"].cost == Decimal(4) and statuses["cc-day"].level == "warning"
    assert statuses["cc-month"].cost == Decimal(8) and statuses["cc-month"].level == "exceeded"
    assert statuses["all-tokens"].tokens == 1_200_000 and statuses["all-tokens"].level == "ok"
    assert statuses["codex"].level == "exceeded"  # 200,000 of 200,000


def test_alerts_are_raised_once_per_period_and_level(db: Database, prices: PriceTable) -> None:
    budgets = [Budget(id="cc-day", agent_type="claude-code", max_cost_usd=Decimal(5))]
    check(db, prices, budgets, UTC, NOW)
    check(db, prices, budgets, UTC, NOW + timedelta(minutes=1))
    (warning,) = AlertStore(db).recent()
    assert warning.level == "warning" and warning.key == "budget:cc-day:2026-10-14:warning"
    assert warning.title == "Budget cc-day reached 80 % of its limit today"
    assert warning.detail == "type claude-code: $4.00 of $5.00"

    UsageStore(db).add_records([rec("4", "cc", NOW, 500_000)], "h-pi")  # +$2: over
    check(db, prices, budgets, UTC, NOW + timedelta(minutes=2))
    assert [a.level for a in AlertStore(db).recent()] == ["exceeded", "warning"]
    check(db, prices, budgets, UTC, NOW + timedelta(days=1, hours=1))  # a new day, still over?
    assert len(AlertStore(db).recent()) == 2  # no usage tomorrow: nothing new


def test_unpriced_usage_makes_cost_a_lower_bound(db: Database, prices: PriceTable) -> None:
    UsageStore(db).add_records([rec("5", "cc", NOW, None, None)], "h-pi")
    (status,) = evaluate(db, prices, [Budget(id="b", agent_id="cc", max_cost_usd=Decimal(5))],
                         UTC, NOW)  # fmt: skip
    assert not status.cost_complete and status.cost == Decimal(4)
    check(db, prices, [status.budget], UTC, NOW)
    assert AlertStore(db).recent()[0].detail == "agent cc: ≥ $4.00 of $5.00"


def test_budgets_page(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    empty: Any = client.get("/budgets").text
    assert "No budgets yet" in empty and "No alerts." in empty
    write(
        tmp_path,
        '[[budgets]]\nid = "cc-day"\nagent_type = "<b>x</b>"\nmax_cost_usd = 5\n'
        '[[budgets]]\nid = "cc"\nagent_type = "claude-code"\nmax_cost_usd = 5\n',
    )
    AlertStore(db).raise_once("k", "exceeded", "Budget <i>x</i> is over", "d", NOW)
    page = client.get("/budgets").text
    assert "&lt;b&gt;x&lt;/b&gt;" in page and "<b>x</b>" not in page
    assert "&lt;i&gt;x&lt;/i&gt;" in page
    assert "<code>cc</code>" in page
    write(tmp_path, "broken")
    assert "ignored" in client.get("/budgets").text
