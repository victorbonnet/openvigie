"""Usage repository: deduplicated usage records, sessions, and how far logs have been read."""

from __future__ import annotations

import json
from typing import Any

from openvigie.hub.store.db import Database, agents_of_host, parse_ts, required_ts, ts
from openvigie.model import Session, UsageBackfill, UsageQuality, UsageRecord

_TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                  "cache_creation_input_tokens", "cache_creation_1h_input_tokens",
                  "reasoning_tokens")  # fmt: skip


def _rank(table: str) -> str:
    return f"(CASE {table}.quality WHEN 'exact' THEN 2 WHEN 'estimated' THEN 1 ELSE 0 END)"


# A more precise replay replaces the counts. One of the same precision can still carry newer
# counts: Claude Code writes one API response over several lines, and a line read in a later
# batch can report more tokens than the one stored. Counts only grow, so keep the larger one
# (NULL is unknown, never smaller); an identical replay changes nothing.
_MORE_PRECISE = f"{_rank('excluded')} > {_rank('usage_records')}"
_SAME_PRECISION = f"{_rank('excluded')} = {_rank('usage_records')}"
_SET_COUNTS = ",\n".join(
    f"{c} = CASE WHEN {_MORE_PRECISE} THEN excluded.{c}"
    f" ELSE coalesce(max(usage_records.{c}, excluded.{c}), usage_records.{c}, excluded.{c}) END"
    for c in _TOKEN_COLUMNS
)
_ANY_LARGER = " OR ".join(
    f"coalesce(excluded.{c} > usage_records.{c}, excluded.{c} IS NOT NULL)" for c in _TOKEN_COLUMNS
)
_UPSERT_RECORD = f"""
    INSERT INTO usage_records (record_id, agent_id, session_id, ts, provider, request_model,
        response_model, {", ".join(_TOKEN_COLUMNS)}, quality)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT (record_id) DO UPDATE SET
        {_SET_COUNTS},
        quality = CASE WHEN {_MORE_PRECISE} THEN excluded.quality ELSE usage_records.quality END
    WHERE usage_records.agent_id = excluded.agent_id
      AND ({_MORE_PRECISE} OR ({_SAME_PRECISION} AND ({_ANY_LARGER})))
"""  # noqa: S608 - built from the column names above, never from input


class UsageStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add_records(self, records: list[UsageRecord], host_id: str) -> int:
        """Insert records for this host's agents; a replay only adds precision or larger counts."""
        changed = 0
        with self.db.transaction() as conn:
            owned = agents_of_host(conn, host_id)
            for r in records:
                if r.agent_id not in owned:
                    continue
                cur = conn.execute(
                    _UPSERT_RECORD,
                    (r.record_id, r.agent_id, r.session_id, ts(r.timestamp), r.provider,
                     r.request_model, r.response_model, r.input_tokens, r.output_tokens,
                     r.cache_read_input_tokens, r.cache_creation_input_tokens,
                     r.cache_creation_1h_input_tokens, r.reasoning_tokens, r.quality.value),
                )  # fmt: skip
                changed += cur.rowcount
        return changed

    def add_sessions(self, sessions: list[Session], host_id: str) -> int:
        changed = 0
        with self.db.transaction() as conn:
            owned = agents_of_host(conn, host_id)
            for s in sessions:
                if s.agent_id not in owned:
                    continue
                row = conn.execute(
                    "SELECT started_at, last_event_at, models FROM sessions "
                    "WHERE agent_id = ? AND session_id = ?",
                    (s.agent_id, s.session_id),
                ).fetchone()
                started = [t for t in (s.started_at and ts(s.started_at),
                                       row and row["started_at"]) if t]  # fmt: skip
                last = [t for t in (s.last_event_at and ts(s.last_event_at),
                                    row and row["last_event_at"]) if t]  # fmt: skip
                models = list(json.loads(row["models"])) if row else []
                models += [m for m in s.models if m not in models]
                conn.execute(
                    "INSERT INTO sessions (agent_id, session_id, started_at, last_event_at, "
                    "models) VALUES (?, ?, ?, ?, ?) ON CONFLICT (agent_id, session_id) DO UPDATE "
                    "SET started_at = excluded.started_at, last_event_at = excluded.last_event_at,"
                    " models = excluded.models",
                    (s.agent_id, s.session_id, min(started) if started else None,
                     max(last) if last else None, json.dumps(models[:32])),
                )  # fmt: skip
                changed += 1
        return changed

    def records(self, agent_id: str | None = None) -> list[UsageRecord]:
        sql = "SELECT * FROM usage_records"
        params: tuple[str, ...] = ()
        if agent_id:
            sql += " WHERE agent_id = ?"
            params = (agent_id,)
        with self.db.read() as conn:
            rows = conn.execute(sql + " ORDER BY ts", params).fetchall()
        return [
            UsageRecord(
                record_id=r["record_id"], agent_id=r["agent_id"],
                timestamp=required_ts(r["ts"]), session_id=r["session_id"], provider=r["provider"],
                request_model=r["request_model"], response_model=r["response_model"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                cache_read_input_tokens=r["cache_read_input_tokens"],
                cache_creation_input_tokens=r["cache_creation_input_tokens"],
                cache_creation_1h_input_tokens=r["cache_creation_1h_input_tokens"],
                reasoning_tokens=r["reasoning_tokens"], quality=UsageQuality(r["quality"]),
            )
            for r in rows
        ]  # fmt: skip

    def sessions(self, agent_id: str) -> list[Session]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE agent_id = ? ORDER BY last_event_at DESC",
                (agent_id,),
            ).fetchall()
        return [
            Session(r["session_id"], r["agent_id"], parse_ts(r["started_at"]),
                    parse_ts(r["last_event_at"]), tuple(json.loads(r["models"])))
            for r in rows
        ]  # fmt: skip

    def set_backfill(self, items: list[UsageBackfill], host_id: str) -> int:
        """Keep the newest report per agent; an older one (e.g. from the spool) is ignored."""
        changed = 0
        with self.db.transaction() as conn:
            owned = agents_of_host(conn, host_id)
            for b in items:
                if b.agent_id not in owned:
                    continue
                cur = conn.execute(
                    "INSERT INTO usage_backfill (agent_id, observed_at, complete, read_until, "
                    "bytes_left) VALUES (?, ?, ?, ?, ?) ON CONFLICT (agent_id) DO UPDATE SET "
                    "observed_at = excluded.observed_at, complete = excluded.complete, "
                    "read_until = excluded.read_until, bytes_left = excluded.bytes_left "
                    "WHERE excluded.observed_at >= usage_backfill.observed_at",
                    (b.agent_id, ts(b.observed_at), int(b.complete),
                     b.read_until and ts(b.read_until), b.bytes_left),
                )  # fmt: skip
                changed += cur.rowcount
        return changed

    def backfill(self, agent_id: str) -> UsageBackfill | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM usage_backfill WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        return None if row is None else backfill_of(row)

    def set_spool_dropped(self, host_id: str, dropped: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE hosts SET spool_dropped = ? WHERE host_id = ?", (dropped, host_id))


def backfill_of(row: Any) -> UsageBackfill:
    """A ``usage_backfill`` row as its wire type."""
    return UsageBackfill(
        agent_id=row["agent_id"],
        observed_at=required_ts(row["observed_at"]),
        complete=bool(row["complete"]),
        read_until=parse_ts(row["read_until"]),
        bytes_left=row["bytes_left"],
    )
