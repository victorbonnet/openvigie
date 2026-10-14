"""Alerts (FR-14, D17): raised once per key, kept forever, delivered by notification channels.

Offers ``AlertStore``: ``raise_once`` (False when the key already exists), ``recent``,
``count_since``, and for channels ``pending`` / ``delivered`` / ``failed``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openvigie.hub.store.db import Database, parse_ts, required_ts, ts

COLUMNS = "id, at, key, level, title, detail, delivered_at, attempts, last_error"


@dataclass(frozen=True)
class Alert:
    id: int
    at: datetime
    key: str
    level: str  # warning | exceeded
    title: str
    detail: str
    delivered_at: datetime | None
    attempts: int
    last_error: str | None


def _row(r: Any) -> Alert:
    return Alert(r[0], required_ts(r[1]), r[2], r[3], r[4], r[5], parse_ts(r[6]), r[7], r[8])


class AlertStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def raise_once(self, key: str, level: str, title: str, detail: str, at: datetime) -> bool:
        with self.db.transaction() as conn:
            return bool(conn.execute(
                "INSERT OR IGNORE INTO alerts (at, key, level, title, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts(at), key[:256], level, title[:256], detail[:1024]),
            ).rowcount)  # fmt: skip

    def recent(self, limit: int = 50) -> list[Alert]:
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM alerts ORDER BY at DESC, id DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def count_since(self, since: datetime) -> int:
        with self.db.read() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM alerts WHERE at >= ?",
                                    (ts(since),)).fetchone()[0])  # fmt: skip

    def pending(self, max_attempts: int, limit: int = 20) -> list[Alert]:
        """Not delivered yet, oldest first, unless they already used up their attempts."""
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM alerts WHERE delivered_at IS NULL AND attempts < ? "  # noqa: S608
                "ORDER BY id LIMIT ?",
                (max_attempts, limit),
            ).fetchall()
        return [_row(r) for r in rows]

    def delivered(self, alert_id: int, at: datetime) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE alerts SET delivered_at = ?, attempts = attempts + 1, "
                         "last_error = NULL WHERE id = ?", (ts(at), alert_id))  # fmt: skip

    def failed(self, alert_id: int, error: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE alerts SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                         (error[:256], alert_id))  # fmt: skip
