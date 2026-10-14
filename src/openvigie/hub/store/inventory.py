"""Inventory repository: hosts, agents, status history, adapter health. Parameterized SQL only."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from openvigie.hub.store.db import Database, parse_ts, required_ts, ts
from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    HealthState,
    Host,
    SandboxState,
    StatusEvent,
)


@dataclass(frozen=True)
class HostRow:
    host: Host
    first_seen: datetime
    last_seen: datetime
    clock_skew_s: float | None
    revoked_at: datetime | None
    spool_dropped: int = 0


@dataclass(frozen=True)
class AgentRow:
    agent: AgentInstance
    first_seen: datetime
    last_seen: datetime
    hostname: str


@dataclass(frozen=True)
class HealthRow:
    host_id: str
    health: AdapterHealth
    updated_at: datetime


class Inventory:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- writes ---------------------------------------------------------------------------

    def upsert_host(self, host: Host, now: datetime, clock_skew_s: float | None = None) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO hosts (host_id, hostname, os, arch, collector_version,
                                   first_seen, last_seen, clock_skew_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (host_id) DO UPDATE SET
                    hostname = excluded.hostname, os = excluded.os, arch = excluded.arch,
                    collector_version = excluded.collector_version,
                    last_seen = excluded.last_seen, clock_skew_s = excluded.clock_skew_s
                """,
                (host.host_id, host.hostname, host.os, host.arch, host.collector_version,
                 ts(now), ts(now), clock_skew_s),
            )  # fmt: skip

    def upsert_agents(self, agents: list[AgentInstance], now: datetime) -> int:
        """Insert or update agents; an existing agent id owned by another host is never touched."""
        changed = 0
        with self.db.transaction() as conn:
            for a in agents:
                cur = conn.execute(
                    """
                    INSERT INTO agents (agent_id, host_id, agent_type, name, version, config_root,
                        sandboxed, sandbox_kind, process_running, last_activity_at, status,
                        first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (agent_id) DO UPDATE SET
                        agent_type = excluded.agent_type, name = excluded.name,
                        version = excluded.version, config_root = excluded.config_root,
                        sandboxed = excluded.sandboxed, sandbox_kind = excluded.sandbox_kind,
                        process_running = excluded.process_running,
                        last_activity_at = excluded.last_activity_at,
                        status = excluded.status, last_seen = excluded.last_seen
                    WHERE agents.host_id = excluded.host_id
                    """,
                    (a.agent_id, a.host_id, a.agent_type, a.name, a.version, a.config_root,
                     a.sandboxed.value, a.sandbox_kind,
                     None if a.process_running is None else int(a.process_running),
                     ts(a.last_activity_at) if a.last_activity_at else None,
                     a.status.value, ts(now), ts(now)),
                )  # fmt: skip
                changed += cur.rowcount
        return changed

    def add_status_events(self, events: list[StatusEvent], host_id: str | None = None) -> int:
        """Store events for known agents (of ``host_id`` when given); duplicates are ignored."""
        added = 0
        with self.db.transaction() as conn:
            for e in events:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO agent_status_events (agent_id, old, new, at) "
                    "SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM agents WHERE agent_id = ? "
                    "AND (? IS NULL OR host_id = ?))",
                    (e.agent_id, e.old.value, e.new.value, ts(e.at), e.agent_id, host_id,
                     host_id),
                )  # fmt: skip
                added += cur.rowcount
        return added

    def set_adapter_health(self, host_id: str, health: list[AdapterHealth], now: datetime) -> None:
        with self.db.transaction() as conn:
            for h in health:
                conn.execute(
                    """
                    INSERT INTO adapter_health (host_id, adapter, state, reason, parse_errors,
                                                skipped, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (host_id, adapter) DO UPDATE SET
                        state = excluded.state, reason = excluded.reason,
                        parse_errors = excluded.parse_errors, skipped = excluded.skipped,
                        updated_at = excluded.updated_at
                    """,
                    (host_id, h.adapter, h.state.value, h.reason, h.parse_errors, h.skipped,
                     ts(now)),
                )  # fmt: skip

    # --- reads ----------------------------------------------------------------------------

    def hosts(self) -> list[HostRow]:
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM hosts ORDER BY hostname, host_id").fetchall()
        return [_host_row(r) for r in rows]

    def host(self, host_id: str) -> HostRow | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        return _host_row(row) if row else None

    def agents(
        self,
        *,
        host_id: str | None = None,
        agent_type: str | None = None,
        status: str | None = None,
    ) -> list[AgentRow]:
        sql = "SELECT a.*, h.hostname FROM agents a JOIN hosts h USING (host_id) WHERE 1 = 1"
        params: list[str] = []
        for column, value in (("a.host_id", host_id), ("a.agent_type", agent_type),
                              ("a.status", status)):  # fmt: skip
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        sql += " ORDER BY h.hostname, a.agent_type, a.config_root"
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_agent_row(r) for r in rows]

    def agent(self, agent_id: str) -> AgentRow | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT a.*, h.hostname FROM agents a JOIN hosts h USING (host_id) "
                "WHERE a.agent_id = ?",
                (agent_id,),
            ).fetchone()
        return _agent_row(row) if row else None

    def status_history(self, agent_id: str, limit: int = 50) -> list[StatusEvent]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_status_events WHERE agent_id = ? ORDER BY at DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        return [
            StatusEvent(r["agent_id"], AgentStatus(r["old"]), AgentStatus(r["new"]),
                        required_ts(r["at"]))
            for r in rows
        ]  # fmt: skip

    def adapter_health(self, host_id: str | None = None) -> list[HealthRow]:
        sql = "SELECT * FROM adapter_health"
        params: tuple[str, ...] = ()
        if host_id is not None:
            sql += " WHERE host_id = ?"
            params = (host_id,)
        with self.db.read() as conn:
            rows = conn.execute(sql + " ORDER BY host_id, adapter", params).fetchall()
        return [
            HealthRow(
                r["host_id"],
                AdapterHealth(r["adapter"], HealthState(r["state"]), r["reason"],
                              r["parse_errors"], r["skipped"]),
                required_ts(r["updated_at"]),
            )
            for r in rows
        ]  # fmt: skip


def _host_row(row: sqlite3.Row) -> HostRow:
    return HostRow(
        host=Host(row["host_id"], row["hostname"], row["os"], row["arch"],
                  row["collector_version"]),
        first_seen=required_ts(row["first_seen"]),
        last_seen=required_ts(row["last_seen"]),
        clock_skew_s=row["clock_skew_s"],
        revoked_at=parse_ts(row["revoked_at"]),
        spool_dropped=row["spool_dropped"],
    )  # fmt: skip


def _agent_row(row: sqlite3.Row) -> AgentRow:
    running = row["process_running"]
    agent = AgentInstance(
        agent_id=row["agent_id"],
        host_id=row["host_id"],
        agent_type=row["agent_type"],
        name=row["name"],
        version=row["version"],
        config_root=row["config_root"],
        sandboxed=SandboxState(row["sandboxed"]),
        sandbox_kind=row["sandbox_kind"],
        process_running=None if running is None else bool(running),
        last_activity_at=parse_ts(row["last_activity_at"]),
        status=AgentStatus(row["status"]),
    )
    return AgentRow(
        agent, required_ts(row["first_seen"]), required_ts(row["last_seen"]), row["hostname"]
    )
