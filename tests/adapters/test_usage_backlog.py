"""``tail_files`` reports what it leaves for later ticks, so the hub can tell history is still
being read (FR-3)."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openvigie.adapters.base import ActivityBatch, AdapterState
from openvigie.adapters.usage import TailBudget, tail_files

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LINE = json.dumps({"pad": "x" * 1000}) + "\n"


def write(path: Path, lines: int, modified: datetime, tail: str = "") -> int:
    path.write_text(LINE * lines + tail)
    os.utime(path, (modified.timestamp(), modified.timestamp()))
    return path.stat().st_size


def tick(root: Path, state: AdapterState) -> tuple[int, ActivityBatch]:
    """One collect with a budget far smaller than the history: about 1 MiB per tick."""
    batch = ActivityBatch()
    budget = TailBudget(remaining=1, per_file=1)
    read = sum(1 for _ in tail_files(root, root, "*.jsonl", state, batch, budget))
    state.commit()
    return read, batch


def test_history_larger_than_a_tick_is_reported_until_read(tmp_path: Path) -> None:
    big = write(tmp_path / "a.jsonl", 2500, NOW - timedelta(days=2))  # read over 3 ticks
    write(tmp_path / "b.jsonl", 10, NOW - timedelta(days=5), tail='{"half": ')  # being written
    state = AdapterState()

    read, batch = tick(tmp_path, state)
    assert batch.backlog is not None and batch.backlog.bytes_left > big // 2
    assert batch.backlog.oldest == NOW - timedelta(days=5)  # b.jsonl is not reached yet
    total = read
    for _ in range(5):
        read, batch = tick(tmp_path, state)
        total += read
        assert batch.backlog is not None
        if not batch.backlog.bytes_left:
            break
    assert total == 2510
    assert (
        batch.backlog is not None and batch.backlog.oldest is None
    )  # complete: the half-written line is not a backlog
    read, batch = tick(tmp_path, state)
    assert read == 0 and batch.backlog is not None and batch.backlog.bytes_left == 0


def test_files_older_than_the_backfill_window_are_not_a_backlog(tmp_path: Path) -> None:
    write(tmp_path / "a.jsonl", 2000, NOW - timedelta(days=1))
    write(tmp_path / "old.jsonl", 2000, NOW - timedelta(days=60))
    state = AdapterState(backfill_since=NOW - timedelta(days=30))
    _, batch = tick(tmp_path, state)
    assert batch.backlog is not None and 0 < batch.backlog.bytes_left < 2000 * len(LINE)
    assert batch.backlog.oldest == NOW - timedelta(days=1)


def test_rotated_file_counts_in_full(tmp_path: Path) -> None:
    big = write(tmp_path / "a.jsonl", 1500, NOW)
    size = write(tmp_path / "b.jsonl", 5, NOW)
    state = AdapterState()
    state.committed["b.jsonl"] = {"dev": 0, "inode": 0, "offset": size, "skip": False}
    _, batch = tick(tmp_path, state)
    assert batch.backlog is not None
    read_of_a = state.committed["a.jsonl"]["offset"]
    assert batch.backlog.bytes_left == big - read_of_a + size  # b.jsonl is a new file now
