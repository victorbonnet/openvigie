"""The audit log (FR-17, D14): every admin action, append-only and hash-chained.

``AuditLog.record`` is the only writer, and there is no update or delete. Each entry stores the
hash of the previous one, and its own hash covers every field plus that link, so editing,
deleting or reordering rows breaks the chain; ``verify`` finds the first break. Values are
redacted before they are hashed and stored: the log never holds raw secrets. Kept forever (D9).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from openvigie.hub.store.db import Database, required_ts, ts
from openvigie.redact import redact_obj

GENESIS = "0" * 64
MAX_VALUE_CHARS = 64 * 1024  # per before/after value, after redaction
FIELDS = ("seq", "at", "actor", "role", "action", "target", "before", "after", "result",
          "request_id", "prev_hash")  # fmt: skip


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    at: str  # ISO 8601 UTC, as stored
    actor: str
    role: str
    action: str
    target: str
    before: str  # canonical JSON
    after: str  # canonical JSON
    result: str
    request_id: str
    prev_hash: str
    hash: str

    @property
    def when(self) -> datetime:
        return required_ts(self.at)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Verification:
    ok: bool
    checked: int
    broken_at: int | None = None  # seq of the first entry that doesn't check out
    reason: str | None = None


def canonical(value: Any) -> str:
    """Redacted, sorted, compact JSON; too-large values are replaced by a marker."""
    text = json.dumps(redact_obj(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)  # fmt: skip
    if len(text) > MAX_VALUE_CHARS:
        return json.dumps({"truncated": True, "chars": len(text)})
    return text


def entry_hash(fields: dict[str, Any]) -> str:
    body = json.dumps({k: fields[k] for k in FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)  # fmt: skip
    return hashlib.sha256((fields["prev_hash"] + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        *,
        at: datetime,
        actor: str,
        role: str,
        action: str,
        target: str,
        result: str,
        request_id: str,
        before: Any = None,
        after: Any = None,
    ) -> AuditEntry:
        """Append one entry. Strings are bounded; values are redacted before hashing."""
        with self.db.transaction() as conn:
            last = conn.execute(
                "SELECT seq, hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            fields: dict[str, Any] = {
                "seq": (last[0] + 1) if last else 1,
                "at": ts(at),
                "actor": actor[:256],
                "role": role[:32],
                "action": action[:128],
                "target": target[:512],
                "before": canonical(before),
                "after": canonical(after),
                "result": result[:64],
                "request_id": request_id[:64],
                "prev_hash": last[1] if last else GENESIS,
            }
            digest = entry_hash(fields)
            conn.execute(
                f"INSERT INTO audit_log ({', '.join(FIELDS)}, hash) "  # noqa: S608 - fixed names
                f"VALUES ({', '.join('?' * (len(FIELDS) + 1))})",
                (*(fields[k] for k in FIELDS), digest),
            )
        return AuditEntry(**fields, hash=digest)

    def entries(self, limit: int = 100, before_seq: int | None = None) -> list[AuditEntry]:
        """Newest first."""
        where, args = ("WHERE seq < ?", (before_seq,)) if before_seq else ("", ())
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(FIELDS)}, hash FROM audit_log {where} "  # noqa: S608
                "ORDER BY seq DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [AuditEntry(*row) for row in rows]

    def all(self) -> Iterator[AuditEntry]:
        """Oldest first, in batches, for verify and export."""
        after = 0
        while True:
            with self.db.read() as conn:
                rows = conn.execute(
                    f"SELECT {', '.join(FIELDS)}, hash FROM audit_log "  # noqa: S608
                    "WHERE seq > ? ORDER BY seq LIMIT 500",
                    (after,),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield AuditEntry(*row)
            after = rows[-1][0]

    def verify(self) -> Verification:
        expected_prev, expected_seq, checked = GENESIS, 1, 0
        for entry in self.all():
            if entry.seq != expected_seq:
                return Verification(False, checked, entry.seq, f"entry {expected_seq} is missing")
            if entry.prev_hash != expected_prev:
                return Verification(False, checked, entry.seq, "link to the previous entry broken")
            if entry_hash(entry.to_json()) != entry.hash:
                return Verification(False, checked, entry.seq, "entry was changed")
            expected_prev, expected_seq, checked = entry.hash, entry.seq + 1, checked + 1
        return Verification(True, checked)
