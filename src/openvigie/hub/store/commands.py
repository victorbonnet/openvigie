"""Commands for collectors (D14): a queue per host, delivered at most once.

``enqueue`` adds a command for a host; ``deliver`` hands that host's due commands to its next
ingest reply and marks them delivered, so a command never runs twice even if the reply is lost
(it then simply gets no result). Commands not delivered before they expire are marked
``expired``. ``record_results`` accepts a result only for a delivered command of the same host,
and only once. Auditing each step is the caller's job (``AuditLog``).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from openvigie.hub.store.db import Database, parse_ts, required_ts, ts
from openvigie.model import Command, CommandResult

MAX_PER_REPLY = 2  # keeps the ingest reply well under the collector's 1 MiB read limit
FINISHED = ("succeeded", "failed", "refused")


@dataclass(frozen=True)
class StoredCommand:
    command_id: str
    host_id: str
    action: str
    args: dict[str, str]
    actor: str
    created_at: datetime
    expires_at: datetime
    status: str  # queued | delivered | expired | succeeded | failed | refused
    delivered_at: datetime | None
    finished_at: datetime | None
    detail: str

    def wire(self) -> Command:
        return Command(self.command_id, self.action, self.expires_at, self.args)


COLUMNS = ("command_id, host_id, action, args, actor, created_at, expires_at, status, "
           "delivered_at, finished_at, detail")  # fmt: skip


def _row(row: Any) -> StoredCommand:
    cid, host, action, args, actor, created, expires, status, delivered, finished, detail = row
    return StoredCommand(cid, host, action, json.loads(args), actor, required_ts(created),
                         required_ts(expires), status, parse_ts(delivered), parse_ts(finished),
                         detail)  # fmt: skip


class CommandQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    def enqueue(
        self,
        host_id: str,
        action: str,
        args: dict[str, str],
        *,
        actor: str,
        now: datetime,
        ttl: timedelta,
        command_id: str | None = None,
    ) -> StoredCommand:
        """Queue a command; the wire type validates action and arguments first."""
        command = Command(command_id or secrets.token_hex(16), action, now + ttl, dict(args))
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO commands ({COLUMNS}) "  # noqa: S608 - fixed names
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, '')",
                (command.command_id, host_id, command.action, json.dumps(command.args), actor,
                 ts(now), ts(command.expires_at)),
            )  # fmt: skip
        return StoredCommand(command.command_id, host_id, command.action, dict(command.args), actor,
                             now, command.expires_at, "queued", None, None, "")  # fmt: skip

    def deliver(
        self, host_id: str, now: datetime
    ) -> tuple[list[StoredCommand], list[StoredCommand]]:
        """The host's due commands, marked delivered, and the ones that expired undelivered."""
        with self.db.transaction() as conn:
            expired = [_row(r) for r in conn.execute(
                f"SELECT {COLUMNS} FROM commands WHERE host_id = ? AND status = 'queued' "  # noqa: S608
                "AND expires_at <= ? ORDER BY created_at", (host_id, ts(now)))]  # fmt: skip
            conn.executemany("UPDATE commands SET status = 'expired' WHERE command_id = ?",
                             [(c.command_id,) for c in expired])  # fmt: skip
            due = [_row(r) for r in conn.execute(
                f"SELECT {COLUMNS} FROM commands WHERE host_id = ? AND status = 'queued' "  # noqa: S608
                "ORDER BY created_at, command_id LIMIT ?", (host_id, MAX_PER_REPLY))]  # fmt: skip
            conn.executemany(
                "UPDATE commands SET status = 'delivered', delivered_at = ? WHERE command_id = ?",
                [(ts(now), c.command_id) for c in due],
            )
        return due, expired

    def record_results(
        self, host_id: str, results: list[CommandResult], now: datetime
    ) -> list[StoredCommand]:
        """Store results for this host's delivered commands; others are ignored."""
        stored: list[StoredCommand] = []
        with self.db.transaction() as conn:
            for result in results:
                updated = conn.execute(
                    "UPDATE commands SET status = ?, finished_at = ?, detail = ? "
                    "WHERE command_id = ? AND host_id = ? AND status = 'delivered'",
                    (result.status.value, ts(now), result.detail, result.command_id, host_id),
                ).rowcount
                if updated:
                    row = conn.execute(
                        f"SELECT {COLUMNS} FROM commands WHERE command_id = ?",  # noqa: S608
                        (result.command_id,),
                    ).fetchone()
                    stored.append(_row(row))
        return stored

    def get(self, command_id: str) -> StoredCommand | None:
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM commands WHERE command_id = ?",  # noqa: S608
                (command_id,),
            ).fetchone()
        return _row(row) if row else None
