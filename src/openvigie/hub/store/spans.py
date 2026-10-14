"""Span export queue (#62): spans wait here until the exporter has sent them to Langfuse."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from openvigie.hub.store.db import Database, ts
from openvigie.hub.store.usage import _owned_agents
from openvigie.model import Span, from_dict, to_dict

DROPPED = "spans_dropped"


@dataclass(frozen=True)
class QueuedSpan:
    row: int
    host_id: str
    span: Span
    attempts: int


@dataclass(frozen=True)
class QueueStats:
    pending: int
    exported: int
    dropped: int


class SpanQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(self, spans: list[Span], host_id: str, now: datetime) -> int:
        """Queue spans of this host's agents; a re-sent span replaces the queued one."""
        queued = 0
        with self.db.transaction() as conn:
            owned = _owned_agents(conn, host_id)
            for span in spans:
                if span.agent_id not in owned:
                    continue
                queued += conn.execute(
                    """
                    INSERT INTO span_export_queue (trace_id, span_id, host_id, span, received_at,
                        next_attempt_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (trace_id, span_id) DO UPDATE SET
                        span = excluded.span, received_at = excluded.received_at,
                        attempts = 0, next_attempt_at = excluded.next_attempt_at,
                        exported_at = NULL
                    WHERE span_export_queue.host_id = excluded.host_id
                    """,
                    (span.trace_id, span.span_id, host_id, json.dumps(to_dict(span)), ts(now),
                     ts(now)),
                ).rowcount  # fmt: skip
        return queued

    def due(self, now: datetime, limit: int) -> list[QueuedSpan]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT rowid, host_id, span, attempts FROM span_export_queue
                WHERE exported_at IS NULL AND next_attempt_at <= ?
                ORDER BY received_at LIMIT ?
                """,
                (ts(now), limit),
            ).fetchall()
        return [QueuedSpan(r[0], r[1], from_dict(Span, json.loads(r[2])), r[3]) for r in rows]

    def mark_exported(self, batch: list[QueuedSpan], now: datetime) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE span_export_queue SET exported_at = ? WHERE rowid = ?",
                [(ts(now), q.row) for q in batch],
            )
            conn.executemany(
                """
                INSERT INTO trace_exports (trace_id, exported_at) VALUES (?, ?)
                ON CONFLICT (trace_id) DO UPDATE SET exported_at = excluded.exported_at
                """,
                [(trace, ts(now)) for trace in {q.span.trace_id for q in batch}],
            )

    def retry_later(self, batch: list[QueuedSpan], at: datetime) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "UPDATE span_export_queue SET attempts = attempts + 1, next_attempt_at = ? "
                "WHERE rowid = ?",
                [(ts(at), q.row) for q in batch],
            )

    def drop(self, batch: list[QueuedSpan]) -> None:
        with self.db.transaction() as conn:
            conn.executemany("DELETE FROM span_export_queue WHERE rowid = ?",
                             [(q.row,) for q in batch])  # fmt: skip
            _count(conn, DROPPED, len(batch))

    def prune(self, cutoff: datetime) -> int:
        """Remove rows received before ``cutoff``; unexported ones count as dropped."""
        with self.db.transaction() as conn:
            lost = conn.execute(
                "SELECT COUNT(*) FROM span_export_queue WHERE received_at < ? "
                "AND exported_at IS NULL",
                (ts(cutoff),),
            ).fetchone()[0]
            removed = conn.execute(
                "DELETE FROM span_export_queue WHERE received_at < ?", (ts(cutoff),)
            ).rowcount
            _count(conn, DROPPED, lost)
        return int(removed)

    def stats(self) -> QueueStats:
        with self.db.read() as conn:
            pending, exported = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE exported_at IS NULL), "
                "COUNT(*) FILTER (WHERE exported_at IS NOT NULL) FROM span_export_queue"
            ).fetchone()
            row = conn.execute("SELECT value FROM hub_counters WHERE name = ?",
                               (DROPPED,)).fetchone()  # fmt: skip
        return QueueStats(pending, exported, row[0] if row else 0)

    def exported(self, trace_ids: list[str]) -> set[str]:
        """Which of these traces reached Langfuse at least once."""
        found: set[str] = set()
        for i in range(0, len(trace_ids), 500):
            chunk = trace_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            with self.db.read() as conn:
                rows = conn.execute(
                    f"SELECT trace_id FROM trace_exports WHERE trace_id IN ({marks})",  # noqa: S608 - placeholders only
                    chunk,
                ).fetchall()
            found.update(r[0] for r in rows)
        return found


def _count(conn: sqlite3.Connection, name: str, amount: int) -> None:
    if amount:
        conn.execute(
            "INSERT INTO hub_counters (name, value) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = value + excluded.value",
            (name, amount),
        )
