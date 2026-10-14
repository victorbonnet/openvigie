from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.collector.state import StateStore
from openvigie.config import CollectorSettings, ConfigError, validate_collector
from openvigie.safeio import FileCursor


def test_cursors_survive_restart_only_after_commit(tmp_path: Path) -> None:
    path = tmp_path / "state" / "collector.db"
    store = StateStore(path)
    state = store.adapter_state("codex")
    state.stage("a.jsonl", FileCursor(1, 2, 300).to_dict())
    store.close()

    store = StateStore(path)  # crash before commit: nothing persisted
    state = store.adapter_state("codex")
    assert state.get("a.jsonl") is None
    state.stage("a.jsonl", FileCursor(1, 2, 300).to_dict())
    state.commit()
    store.close()

    store = StateStore(path)
    state = store.adapter_state("codex")
    assert FileCursor.from_dict(state.get("a.jsonl") or {}) == FileCursor(1, 2, 300)
    assert store.adapter_state("claude-code").get("a.jsonl") is None
    assert path.stat().st_mode & 0o777 == 0o600


def test_rollback_keeps_last_committed(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    state = store.adapter_state("codex")
    state.stage("f", {"offset": 1})
    state.commit()
    state.stage("f", {"offset": 2})
    state.rollback()
    assert store.adapter_state("codex").get("f") == {"offset": 1}


def test_backfill_since_is_exposed(tmp_path: Path) -> None:
    since = datetime(2026, 9, 14, tzinfo=UTC)
    assert StateStore(tmp_path / "s.db").adapter_state("x", since).backfill_since == since


def test_spool_is_fifo(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db")
    for n in range(3):
        store.spool_put({"n": n})
    items = store.spool_peek(limit=10)
    assert [payload["n"] for _, payload in items] == [0, 1, 2]
    store.spool_ack(items[0][0])
    assert [p["n"] for _, p in store.spool_peek(limit=10)] == [1, 2]
    assert store.spool_stats()[0] == 2


def test_spool_cap_drops_oldest_and_counts(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "s.db", spool_limit=1000)
    for n in range(10):
        store.spool_put({"n": n, "pad": "x" * 200})
    count, size = store.spool_stats()
    assert size <= 1000
    remaining = [p["n"] for _, p in store.spool_peek(limit=10)]
    assert remaining == list(range(10 - count, 10))
    assert store.dropped() == 10 - count
    store.spool_put({"pad": "y" * 5000})  # larger than the whole spool: dropped outright
    assert store.dropped() == 10 - count + 1


def test_spool_survives_restart(tmp_path: Path) -> None:
    StateStore(tmp_path / "s.db").spool_put({"n": 1})
    assert StateStore(tmp_path / "s.db").spool_peek()[0][1] == {"n": 1}


def test_settings_validation() -> None:
    assert CollectorSettings().backfill_days == 30
    with pytest.raises(ConfigError):
        validate_collector(CollectorSettings(backfill_days=-1))
    with pytest.raises(ConfigError):
        validate_collector(CollectorSettings(spool_limit_mb=0))
