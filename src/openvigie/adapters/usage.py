"""Helpers for adapters that read usage incrementally from JSONL logs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openvigie.adapters.base import ActivityBatch, AdapterState
from openvigie.safeio import FileCursor, UnsafeFileError, tail_jsonl, walk_bounded

MiB = 1024 * 1024


@dataclass
class TailBudget:
    """Bytes one ``collect`` call may read across all files; the rest waits for the next tick."""

    remaining: int = 16 * MiB
    per_file: int = 4 * MiB


def int_or_none(value: Any) -> int | None:
    """A token count, or ``None`` when absent or nonsensical (unknown is not zero)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53:
        return None
    return value


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def tail_files(
    root: Path,
    directory: Path,
    pattern: str,
    state: AdapterState,
    batch: ActivityBatch,
    budget: TailBudget,
    *,
    max_depth: int = 6,
) -> Iterator[Any]:
    """Yield new JSON values from every matching file, staging a cursor per file.

    Files untouched since their cursor are skipped with a ``stat`` only. On a first read, files
    last modified before ``state.backfill_since`` are skipped entirely (cursor set to the end).
    """
    for path in walk_bounded(directory, pattern, max_depth=max_depth):
        if budget.remaining <= 0:
            return
        key = str(path.relative_to(root))
        saved = state.get(key)
        cursor = FileCursor.from_dict(saved) if saved else None
        try:
            st = path.stat()
        except OSError:
            continue
        if cursor and (cursor.dev, cursor.inode, cursor.offset) == (
            st.st_dev,
            st.st_ino,
            st.st_size,
        ):
            continue
        since = state.backfill_since
        if cursor is None and since and datetime.fromtimestamp(st.st_mtime, UTC) < since:
            state.stage(key, FileCursor(st.st_dev, st.st_ino, st.st_size).to_dict())
            continue
        try:
            result = tail_jsonl(path, cursor, budget=min(budget.per_file, budget.remaining))
        except (OSError, UnsafeFileError):
            batch.stats.parse_errors += 1
            continue
        start = 0 if result.reset or cursor is None else cursor.offset
        budget.remaining -= max(0, result.cursor.offset - start)
        batch.stats.add(result.stats)
        yield from result.values
        state.stage(key, result.cursor.to_dict())
