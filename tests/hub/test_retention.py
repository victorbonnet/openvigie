from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import ConfigError, HubSettings, Retention, load_hub_settings
from openvigie.hub import retention as retention_mod
from openvigie.hub.app import create_app
from openvigie.hub.reports import usage_report
from openvigie.hub.retention import prune
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.usage import UsageStore
from openvigie.model import (
    AgentInstance,
    AgentStatus,
    Host,
    Session,
    StatusEvent,
    UsageQuality,
    UsageRecord,
)
from openvigie.pricing import load_price_table

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")


def seeded(now: datetime = NOW) -> Database:
    """Usage every 10 days back from ``now``, and status events 1, 50 and 200 days back."""
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, now)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "codex", "Codex")], now)
    store = UsageStore(db)
    store.add_records(
        [UsageRecord(f"r{d}", "a1", now - timedelta(days=d), session_id=f"s{d}",
                     provider="anthropic", response_model="claude-opus-5-5",
                     input_tokens=1_000_000, output_tokens=0, quality=UsageQuality.EXACT)
         for d in range(0, 400, 10)],
        "h-pi",
    )  # fmt: skip
    store.add_sessions(
        [Session(f"s{d}", "a1", now - timedelta(days=d), now - timedelta(days=d))
         for d in range(0, 400, 10)],
        "h-pi",
    )  # fmt: skip
    inv.add_status_events(
        [StatusEvent("a1", AgentStatus.IDLE, AgentStatus.ACTIVE, now - timedelta(days=d))
         for d in (1, 50, 200)]
    )  # fmt: skip
    return db


def test_default_keeps_everything() -> None:
    db = seeded()
    assert prune(db, Retention(), NOW) == retention_mod.PruneResult(0, 0, 0)
    assert len(UsageStore(db).records()) == 40


def test_prune_old_rows_and_keep_recent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(retention_mod, "BATCH", 3)  # exercise batching
    db = seeded()
    result = prune(db, Retention(usage_days=180, status_events_days=30), NOW)
    assert result.usage_records == 21 and result.sessions == 21 and result.status_events == 2
    remaining = UsageStore(db).records()
    assert len(remaining) == 19  # the record exactly 180 days old is on the cutoff: kept
    assert min(r.timestamp for r in remaining) >= NOW - timedelta(days=180)
    assert len(Inventory(db).status_history("a1")) == 1
    prices = load_price_table(tmp_path / "x.toml")
    start, end = NOW - timedelta(days=500), NOW + timedelta(days=1)
    total = usage_report(db, prices, start=start, end=end).total
    assert total.records == 19  # aggregates stay consistent after pruning


def test_retention_config(tmp_path: Path) -> None:
    path = tmp_path / "hub.toml"
    path.write_text("[retention]\nusage_days = 180\n")
    assert load_hub_settings(path).retention == Retention(usage_days=180)
    path.write_text("[retention]\nusage_days = -1\n")
    with pytest.raises(ConfigError):
        load_hub_settings(path)
    path.write_text("[retention]\nusage_dayz = 1\n")
    with pytest.raises(ConfigError, match="usage_dayz"):
        load_hub_settings(path)


def test_runs_at_startup_and_settings_page() -> None:
    db = seeded(datetime.now(UTC))  # the startup run prunes against the real clock
    settings = HubSettings(retention=Retention(usage_days=1))
    with TestClient(create_app(settings, db), base_url="http://127.0.0.1:7878") as client:
        html = client.get("/settings").text
        assert "Usage retention</dt><dd>1 days" in html
        assert "Status history retention</dt><dd>kept forever" in html
        for _ in range(100):  # the startup run happens in a worker thread
            if len(UsageStore(db).records()) == 1:
                break
            __import__("time").sleep(0.02)
    assert len(UsageStore(db).records()) == 1
