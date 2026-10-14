"""Enrolment codes and per-host collector tokens. Only SHA-256 hashes are stored."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from openvigie.hub.store.db import Database, parse_ts, ts

TOKEN_PREFIX = "ovh_"  # noqa: S105 - a public prefix, not a secret
CODE_PREFIX = "ove_"
TOUCH_INTERVAL = timedelta(minutes=1)


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class Credentials:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_enrol_code(
        self, now: datetime, ttl: timedelta, bound_host_id: str | None = None
    ) -> str:
        code = CODE_PREFIX + secrets.token_urlsafe(24)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO enrol_codes (code_hash, created_at, expires_at, bound_host_id) "
                "VALUES (?, ?, ?, ?)",
                (digest(code), ts(now), ts(now + ttl), bound_host_id),
            )
        return code

    def consume_enrol_code(self, code: str, host_id: str, now: datetime) -> bool:
        """Atomically mark a valid, unexpired, unused code as used. Single use."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE enrol_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL "
                "AND expires_at > ? AND (bound_host_id IS NULL OR bound_host_id = ?)",
                (ts(now), digest(code), ts(now), host_id),
            )
            return cur.rowcount == 1

    def code_usable(self, code: str, now: datetime) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM enrol_codes WHERE code_hash = ? AND used_at IS NULL "
                "AND expires_at > ?",
                (digest(code), ts(now)),
            ).fetchone()
        return row is not None

    def code_bound_to(self, code: str, host_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM enrol_codes WHERE code_hash = ? AND bound_host_id = ?",
                (digest(code), host_id),
            ).fetchone()
        return row is not None

    def has_active_token(self, host_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM host_tokens WHERE host_id = ? AND revoked_at IS NULL", (host_id,)
            ).fetchone()
        return row is not None

    def issue_token(self, host_id: str, now: datetime) -> str:
        """Issue a new token for ``host_id`` and revoke its previous ones."""
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE host_tokens SET revoked_at = ? WHERE host_id = ? AND revoked_at IS NULL",
                (ts(now), host_id),
            )
            conn.execute(
                "INSERT INTO host_tokens (token_hash, host_id, created_at) VALUES (?, ?, ?)",
                (digest(token), host_id, ts(now)),
            )
        return token

    def verify_token(self, token: str, now: datetime) -> str | None:
        """Return the host id for a valid token, else ``None``."""
        if not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        wanted = digest(token)
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT t.token_hash, t.host_id, t.last_used_at FROM host_tokens t "
                "JOIN hosts h USING (host_id) "
                "WHERE t.token_hash = ? AND t.revoked_at IS NULL AND h.revoked_at IS NULL",
                (wanted,),
            ).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], wanted):
            return None
        last = parse_ts(row["last_used_at"])
        if last is None or now - last > TOUCH_INTERVAL:
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE host_tokens SET last_used_at = ? WHERE token_hash = ?",
                    (ts(now), wanted),
                )
        return str(row["host_id"])

    def revoke_host(self, host_id: str, now: datetime) -> bool:
        """Invalidate every token of ``host_id`` and mark the host revoked (data is kept)."""
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE hosts SET revoked_at = ? WHERE host_id = ? AND revoked_at IS NULL",
                (ts(now), host_id),
            ).rowcount
            conn.execute(
                "UPDATE host_tokens SET revoked_at = ? WHERE host_id = ? AND revoked_at IS NULL",
                (ts(now), host_id),
            )
        return changed == 1

    def is_revoked(self, host_id: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT revoked_at FROM hosts WHERE host_id = ?", (host_id,)
            ).fetchone()
        return row is not None and row["revoked_at"] is not None

    def reinstate(self, host_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE hosts SET revoked_at = NULL WHERE host_id = ?", (host_id,))

    def token_info(self) -> dict[str, tuple[datetime | None, datetime | None]]:
        """host_id -> (created_at, last_used_at) of its active token."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT host_id, created_at, last_used_at FROM host_tokens WHERE revoked_at IS NULL"
            ).fetchall()
        return {
            r["host_id"]: (parse_ts(r["created_at"]), parse_ts(r["last_used_at"])) for r in rows
        }
