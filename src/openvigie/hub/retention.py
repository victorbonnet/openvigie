"""Configurable data retention (PRD Security → "configurable retention").

Runs once at startup and then daily inside the hub process (no cron needed). Deletes in small
batches so the UI and ingest never wait long on the database lock, then returns freed pages to
the file system. ``0`` days keeps data forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from openvigie.config import Retention
from openvigie.hub.store.db import Database, ts

log = logging.getLogger(__name__)

BATCH = 5000
INTERVAL_S = 24 * 3600


@dataclass(frozen=True)
class PruneResult:
    usage_records: int = 0
    sessions: int = 0
    status_events: int = 0


def _delete_batched(db: Database, table: str, column: str, cutoff: str) -> int:
    total = 0
    while True:
        with db.transaction() as conn:
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "  # noqa: S608 - constant identifiers
                f"(SELECT rowid FROM {table} WHERE {column} < ? LIMIT ?)",
                (cutoff, BATCH),
            ).rowcount
        total += deleted
        if deleted < BATCH:
            return total


def prune(db: Database, retention: Retention, now: datetime | None = None) -> PruneResult:
    now = now or datetime.now(UTC)
    usage = sessions = events = 0
    if retention.usage_days:
        cutoff = ts(now - timedelta(days=retention.usage_days))
        usage = _delete_batched(db, "usage_records", "ts", cutoff)
        sessions = _delete_batched(db, "sessions", "last_event_at", cutoff)
    if retention.status_events_days:
        cutoff = ts(now - timedelta(days=retention.status_events_days))
        events = _delete_batched(db, "agent_status_events", "at", cutoff)
    if usage or sessions or events:
        with db.read() as conn:
            conn.execute("PRAGMA incremental_vacuum(2000)")
        log.info("retention: removed %d usage records, %d sessions, %d status events",
                 usage, sessions, events)  # fmt: skip
    return PruneResult(usage, sessions, events)


@asynccontextmanager
async def retention_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prune at startup, then every 24 h, in a worker thread."""

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(prune, app.state.db, app.state.settings.retention)
            except Exception:
                log.exception("retention run failed")
            await asyncio.sleep(INTERVAL_S)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
