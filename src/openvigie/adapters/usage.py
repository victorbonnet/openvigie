"""Helpers for adapters that read usage incrementally from JSONL logs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openvigie.adapters.base import ActivityBatch, AdapterState, Backlog, HostContext
from openvigie.safeio import FileCursor, UnsafeFileError, tail_jsonl, walk_bounded

MiB = 1024 * 1024


@dataclass
class TailBudget:
    """Bytes one ``collect`` call may read across all files; the rest waits for the next tick."""

    remaining: int = 16 * MiB
    per_file: int = 4 * MiB

    @classmethod
    def for_context(cls, ctx: HostContext) -> TailBudget:
        """Spans cost far more memory per line than usage: read less per tick when tracing."""
        return cls(remaining=4 * MiB, per_file=2 * MiB) if ctx.traces.enabled else cls()


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


def before_backfill(entry: Any, state: AdapterState) -> bool:
    """Whether a log entry is older than the first-run backfill window."""
    when = parse_time(entry.get("timestamp")) if isinstance(entry, dict) else None
    return bool(state.backfill_since and when and when < state.backfill_since)


def tail_files(
    root: Path,
    directory: Path,
    pattern: str,
    state: AdapterState,
    batch: ActivityBatch,
    budget: TailBudget,
    *,
    max_depth: int = 6,
) -> Iterator[tuple[str, Any]]:
    """Yield ``(file key, JSON value)`` for new lines in every matching file.

    A cursor is staged per file once its new lines have been consumed.

    Files untouched since their cursor are skipped with a ``stat`` only. On a first read, files
    last modified before ``state.backfill_since`` are skipped entirely (cursor set to the end).
    Once the budget is spent, the remaining files are only ``stat``-ed: what they still hold
    goes into ``batch.backlog``, so the hub can tell history is still being read.
    """
    backlog = batch.backlog = Backlog()
    for path in walk_bounded(directory, pattern, max_depth=max_depth):
        key = str(path.relative_to(root))
        saved = state.get(key)
        cursor = FileCursor.from_dict(saved) if saved else None
        try:
            st = path.stat()
        except OSError:
            continue
        same_file = cursor is not None and (cursor.dev, cursor.inode) == (st.st_dev, st.st_ino)
        if cursor and same_file and cursor.offset == st.st_size:
            continue
        modified = datetime.fromtimestamp(st.st_mtime, UTC)
        since = state.backfill_since
        if cursor is None and since and modified < since:
            state.stage(key, FileCursor(st.st_dev, st.st_ino, st.st_size).to_dict())
            continue
        if budget.remaining <= 0:
            done = cursor.offset if cursor and same_file and cursor.offset <= st.st_size else 0
            backlog.add(st.st_size - done, modified)
            continue
        try:
            result = tail_jsonl(path, cursor, budget=min(budget.per_file, budget.remaining))
        except (OSError, UnsafeFileError):
            batch.stats.parse_errors += 1
            continue
        start = 0 if result.reset or cursor is None else cursor.offset
        budget.remaining -= max(0, result.cursor.offset - start)
        if result.more:
            backlog.add(st.st_size - result.cursor.offset, modified)
        batch.stats.add(result.stats)
        for value in result.values:
            yield key, value
        state.stage(key, result.cursor.to_dict())
