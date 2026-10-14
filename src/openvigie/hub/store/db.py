"""SQLite connection and forward-only migrations for the hub (stdlib ``sqlite3``, no ORM)."""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from openvigie.config import ensure_private_dir

MEMORY = ":memory:"


class StoreError(RuntimeError):
    pass


def ts(when: datetime) -> str:
    """Fixed-width UTC timestamp, so string order is time order."""
    if when.tzinfo is None:
        raise ValueError("naive datetime")
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def required_ts(value: str) -> datetime:
    """A NOT NULL timestamp column; raising guards against schema drift."""
    parsed = parse_ts(value)
    if parsed is None:
        raise ValueError("missing timestamp")
    return parsed


def agents_of_host(conn: sqlite3.Connection, host_id: str) -> set[str]:
    """Ids of the agents a host owns: a host may only write data for these."""
    rows = conn.execute("SELECT agent_id FROM agents WHERE host_id = ?", (host_id,)).fetchall()
    return {row[0] for row in rows}


def migrations() -> list[tuple[int, str, str]]:
    folder = resources.files("openvigie.hub.store") / "migrations"
    found = []
    for entry in folder.iterdir():
        name = entry.name
        if name.endswith(".sql") and name[:4].isdigit():
            found.append((int(name[:4]), name, entry.read_text(encoding="utf-8")))
    found.sort()
    numbers = [n for n, _, _ in found]
    if numbers != list(range(1, len(found) + 1)):
        raise StoreError(f"migrations must be numbered 1..n without gaps, got {numbers}")
    return found


def _statements(script: str) -> Iterator[str]:
    buffer = ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--"):
            continue
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                yield buffer.strip()
            buffer = ""
    if buffer.strip():
        raise StoreError("migration ends with an incomplete statement")


class Database:
    """One shared connection guarded by a lock; fine for a single-node hub on a Pi."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != MEMORY:
            db_path = Path(self.path)
            ensure_private_dir(db_path.parent)
            if not db_path.exists():
                os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != MEMORY:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        self.migrate()
        self._restrict_files()

    def _restrict_files(self) -> None:
        if self.path == MEMORY:
            return
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.path + suffix)
            if path.exists():
                path.chmod(0o600)

    @property
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        available = migrations()
        current = self.version
        if current > len(available):
            raise StoreError(
                f"database schema v{current} is newer than this openvigie (v{len(available)}); "
                "upgrade openvigie"
            )
        for number, _name, script in available[current:]:
            with self.transaction() as conn:
                for statement in _statements(script):
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(number)}")
        return self.version

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            yield self.conn

    def close(self) -> None:
        with self.lock:
            self.conn.close()
