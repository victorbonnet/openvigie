"""Collector-side durable state: adapter cursors and an outbound spool (stdlib ``sqlite3``).

Cursors make restarts cheap: adapters resume where they stopped instead of re-reading logs.
They are committed only once the data derived from them is safe, either acknowledged by the hub
or written to the spool, so delivery is at-least-once (the hub deduplicates).
The spool holds payloads the hub could not take yet. It is bounded: when full, the oldest
payloads are dropped and counted, and the count is reported to the hub.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from openvigie.adapters.base import AdapterState
from openvigie.config import ensure_private_dir

DEFAULT_SPOOL_LIMIT = 50 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    adapter TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (adapter, key)
);
CREATE TABLE IF NOT EXISTS spool (
    id INTEGER PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
"""


class StateStore:
    def __init__(self, path: Path, spool_limit: int = DEFAULT_SPOOL_LIMIT) -> None:
        ensure_private_dir(path.parent)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.spool_limit = spool_limit

    def close(self) -> None:
        self.conn.close()

    # --- cursors ---------------------------------------------------------------------------

    def load_cursors(self, adapter: str) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT key, value FROM cursors WHERE adapter = ?", (adapter,)
            ).fetchall()
        return {key: json.loads(value) for key, value in rows}

    def save_cursors(self, adapter: str, cursors: dict[str, dict[str, Any]]) -> None:
        with self.lock, self.conn:
            self.conn.executemany(
                "INSERT INTO cursors (adapter, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (adapter, key) DO UPDATE SET value = excluded.value",
                [(adapter, k, json.dumps(v)) for k, v in cursors.items()],
            )

    def adapter_state(
        self, adapter: str, backfill_since: datetime | None = None
    ) -> PersistentAdapterState:
        return PersistentAdapterState(self, adapter, backfill_since)

    # --- spool -----------------------------------------------------------------------------

    def spool_put(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":"))
        size = len(text.encode())
        with self.lock, self.conn:
            if size > self.spool_limit:
                self._bump_dropped(1)
                return
            self.conn.execute(
                "INSERT INTO spool (created_at, payload, size) VALUES (?, ?, ?)",
                (time.time(), text, size),
            )
            total = self.conn.execute("SELECT COALESCE(SUM(size), 0) FROM spool").fetchone()[0]
            dropped = 0
            while total > self.spool_limit:
                oldest_id, oldest_size = self.conn.execute(
                    "SELECT id, size FROM spool ORDER BY id LIMIT 1"
                ).fetchone()
                self.conn.execute("DELETE FROM spool WHERE id = ?", (oldest_id,))
                total -= oldest_size
                dropped += 1
            if dropped:
                self._bump_dropped(dropped)

    def spool_peek(self, limit: int = 1) -> list[tuple[int, dict[str, Any]]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, payload FROM spool ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [(row_id, json.loads(payload)) for row_id, payload in rows]

    def spool_ack(self, row_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM spool WHERE id = ?", (row_id,))

    def spool_stats(self) -> tuple[int, int]:
        with self.lock:
            count, size = self.conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM spool"
            ).fetchone()
        return int(count), int(size)

    def dropped(self) -> int:
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'dropped'").fetchone()
        return int(row[0]) if row else 0

    def _bump_dropped(self, n: int) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES ('dropped', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = value + excluded.value",
            (n,),
        )


class PersistentAdapterState(AdapterState):
    """``AdapterState`` whose committed cursors survive restarts."""

    def __init__(self, store: StateStore, adapter: str, backfill_since: datetime | None) -> None:
        super().__init__(store.load_cursors(adapter), backfill_since=backfill_since)
        self.store = store
        self.adapter = adapter

    def commit(self) -> None:
        if self.staged:
            self.store.save_cursors(self.adapter, self.staged)
        super().commit()
