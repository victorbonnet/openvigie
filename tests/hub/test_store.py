import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openvigie.hub.store import db as dbmod
from openvigie.hub.store.db import Database, StoreError, ts
from openvigie.hub.store.inventory import Inventory
from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    HealthState,
    Host,
    SandboxState,
    StatusEvent,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.1")


def agent(agent_id: str = "a1", host: Host = PI, **kw: object) -> AgentInstance:
    base = AgentInstance(
        agent_id,
        host.host_id,
        "claude-code",
        "Claude Code",
        version="2.1.0",
        config_root="~/.claude",
        sandboxed=SandboxState.NO,
        process_running=True,
        last_activity_at=NOW,
        status=AgentStatus.ACTIVE,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


@pytest.fixture
def inv(tmp_path: Path) -> Inventory:
    return Inventory(Database(tmp_path / "hub" / "hub.db"))


def test_fresh_database_is_migrated_and_private(tmp_path: Path) -> None:
    db = Database(tmp_path / "data" / "hub.db")
    assert db.version == len(dbmod.migrations())
    assert (tmp_path / "data" / "hub.db").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
    assert db.migrate() == db.version  # re-running is a no-op
    db.close()


def test_database_newer_than_code_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    Database(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 999")
    with pytest.raises(StoreError, match="newer than this openvigie"):
        Database(path)


def test_failed_migration_rolls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    good = dbmod.migrations()
    bad = [*good, (len(good) + 1, "9999_bad.sql", "CREATE TABLE ok (x);\nNOT SQL;\n")]
    monkeypatch.setattr(dbmod, "migrations", lambda: bad)
    with pytest.raises(sqlite3.OperationalError):
        Database(dbmod.MEMORY)


def test_timestamps_sort_lexically() -> None:
    a = ts(NOW)
    b = ts(NOW + timedelta(microseconds=1))
    assert a < b and len(a) == len(b)
    with pytest.raises(ValueError):
        ts(datetime(2026, 1, 1))


def test_upsert_host_and_agent_is_idempotent(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.upsert_host(PI, NOW + timedelta(seconds=5), clock_skew_s=1.5)
    assert inv.upsert_agents([agent()], NOW) == 1
    assert inv.upsert_agents([agent(status=AgentStatus.IDLE)], NOW + timedelta(seconds=5)) == 1
    (host,) = inv.hosts()
    assert host.first_seen == NOW and host.clock_skew_s == 1.5
    (row,) = inv.agents()
    assert row.agent.status is AgentStatus.IDLE
    assert row.first_seen == NOW and row.last_seen == NOW + timedelta(seconds=5)
    assert row.hostname == "pi5"
    assert row.agent.last_activity_at == NOW


def test_agent_owned_by_other_host_is_not_overwritten(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.upsert_host(MAC, NOW)
    inv.upsert_agents([agent("a1", PI)], NOW)
    assert inv.upsert_agents([agent("a1", MAC, name="hijack")], NOW) == 0
    row = inv.agent("a1")
    assert row is not None and row.agent.host_id == "h-pi" and row.agent.name == "Claude Code"


def test_filters_and_unknowns(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.upsert_host(MAC, NOW)
    inv.upsert_agents(
        [agent("a1", PI), agent("a2", MAC, agent_type="codex", process_running=None,
                                version=None, status=AgentStatus.UNKNOWN)],
        NOW,
    )  # fmt: skip
    assert [r.agent.agent_id for r in inv.agents(host_id="h-mac")] == ["a2"]
    assert [r.agent.agent_id for r in inv.agents(agent_type="claude-code")] == ["a1"]
    (unknown,) = inv.agents(status="unknown")
    assert unknown.agent.process_running is None and unknown.agent.version is None
    assert inv.agent("missing") is None


def test_status_history_dedupes_and_orders(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.upsert_agents([agent()], NOW)
    events = [
        StatusEvent("a1", AgentStatus.UNKNOWN, AgentStatus.ACTIVE, NOW),
        StatusEvent("a1", AgentStatus.ACTIVE, AgentStatus.IDLE, NOW + timedelta(minutes=6)),
        StatusEvent("ghost", AgentStatus.UNKNOWN, AgentStatus.ACTIVE, NOW),
    ]
    assert inv.add_status_events(events) == 2
    assert inv.add_status_events(events) == 0
    history = inv.status_history("a1")
    assert [e.new for e in history] == [AgentStatus.IDLE, AgentStatus.ACTIVE]


def test_adapter_health_upsert(inv: Inventory) -> None:
    inv.upsert_host(PI, NOW)
    inv.set_adapter_health("h-pi", [AdapterHealth("codex", HealthState.OK)], NOW)
    inv.set_adapter_health(
        "h-pi", [AdapterHealth("codex", HealthState.DEGRADED, "bad toml", 2)], NOW
    )
    (row,) = inv.adapter_health("h-pi")
    assert row.health.state is HealthState.DEGRADED and row.health.parse_errors == 2
