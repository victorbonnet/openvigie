"""Usage repository: deduplicated usage records and sessions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from openvigie.hub.store.db import Database, parse_ts, ts
from openvigie.model import Session, UsageQuality, UsageRecord


class UsageStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add_records(self, records: list[UsageRecord], host_id: str) -> int:
        """Insert records for this host's agents; a replay only upgrades less precise data."""
        changed = 0
        with self.db.transaction() as conn:
            owned = _owned_agents(conn, host_id)
            for r in records:
                if r.agent_id not in owned:
                    continue
                cur = conn.execute(
                    """
                    INSERT INTO usage_records (record_id, agent_id, session_id, ts, provider,
                        request_model, response_model, input_tokens, output_tokens,
                        cache_read_input_tokens, cache_creation_input_tokens, reasoning_tokens,
                        quality)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (record_id) DO UPDATE SET
                        input_tokens = excluded.input_tokens,
                        output_tokens = excluded.output_tokens,
                        cache_read_input_tokens = excluded.cache_read_input_tokens,
                        cache_creation_input_tokens = excluded.cache_creation_input_tokens,
                        reasoning_tokens = excluded.reasoning_tokens,
                        quality = excluded.quality
                    WHERE usage_records.agent_id = excluded.agent_id
                      AND (CASE excluded.quality WHEN 'exact' THEN 2 WHEN 'estimated' THEN 1
                           ELSE 0 END)
                        > (CASE usage_records.quality WHEN 'exact' THEN 2 WHEN 'estimated' THEN 1
                           ELSE 0 END)
                    """,
                    (r.record_id, r.agent_id, r.session_id, ts(r.timestamp), r.provider,
                     r.request_model, r.response_model, r.input_tokens, r.output_tokens,
                     r.cache_read_input_tokens, r.cache_creation_input_tokens,
                     r.reasoning_tokens, r.quality.value),
                )  # fmt: skip
                changed += cur.rowcount
        return changed

    def add_sessions(self, sessions: list[Session], host_id: str) -> int:
        changed = 0
        with self.db.transaction() as conn:
            owned = _owned_agents(conn, host_id)
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
                timestamp=_dt(r["ts"]), session_id=r["session_id"], provider=r["provider"],
                request_model=r["request_model"], response_model=r["response_model"],
                input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
                cache_read_input_tokens=r["cache_read_input_tokens"],
                cache_creation_input_tokens=r["cache_creation_input_tokens"],
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

    def set_spool_dropped(self, host_id: str, dropped: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE hosts SET spool_dropped = ? WHERE host_id = ?", (dropped, host_id))


def _owned_agents(conn: sqlite3.Connection, host_id: str) -> set[str]:
    rows = conn.execute("SELECT agent_id FROM agents WHERE host_id = ?", (host_id,)).fetchall()
    return {row[0] for row in rows}


def _dt(value: str) -> datetime:
    parsed = parse_ts(value)
    if parsed is None:
        raise ValueError("missing timestamp")
    return parsed
