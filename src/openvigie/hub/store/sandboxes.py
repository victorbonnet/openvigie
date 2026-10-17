"""OpenShell sandboxes per host, the history of what changed in them (FR-10), and the
connections each one blocked (FR-18)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase

from openvigie.hub.store.db import Database, required_ts, ts
from openvigie.model import BlockedRequest, OpenShellSandbox, SandboxRule, from_dict, to_dict

MAX_CHANGES = 100  # per snapshot: a wholesale rewrite is summarised, not logged rule by rule


@dataclass(frozen=True)
class SandboxChange:
    at: datetime
    sandbox_id: str
    name: str
    change: str  # created | deleted | added | removed | changed
    detail: str


@dataclass(frozen=True)
class Blocked:
    """Connections one sandbox blocked from one program to one destination (FR-18)."""

    sandbox_id: str
    binary: str
    host: str
    port: int
    count: int
    first_seen: datetime
    last_seen: datetime
    reason: str

    @property
    def key(self) -> str:
        """A short id for links (the *Allow…* editor)."""
        return hashlib.sha256(f"{self.binary}\0{self.host}\0{self.port}".encode()).hexdigest()[:16]


def allows(rules: Sequence[SandboxRule], binary: str, host: str, port: int) -> bool:
    """Whether a rule lets ``binary`` reach ``host:port``; binaries and hosts may be globs."""

    def reaches(rule: SandboxRule) -> bool:
        return any(e.host and fnmatchcase(host.lower(), e.host.lower()) and port in e.ports
                   for e in rule.endpoints)  # fmt: skip

    return any(any(fnmatchcase(binary, b) for b in r.binaries) and reaches(r) for r in rules)


def _rule(rule: SandboxRule) -> str:
    endpoints = ", ".join(
        f"{e.host or '?'}:{'/'.join(map(str, e.ports)) or '*'}"
        f"{f' {e.access}' if e.access else ''}{' (audit)' if e.enforcement != 'enforce' else ''}"
        for e in rule.endpoints
    )
    return f"{endpoints or 'no endpoints'} for {', '.join(rule.binaries) or 'no binaries'}"


def diff(old: OpenShellSandbox, new: OpenShellSandbox) -> list[tuple[str, str]]:
    """(change, detail) for what differs between two snapshots of one sandbox."""
    out: list[tuple[str, str]] = []
    for name, label in (("phase", "phase"), ("policy_version", "policy revision"),
                        ("agent_type", "agent type")):  # fmt: skip
        before, after = getattr(old, name), getattr(new, name)
        if before != after:
            out.append(("changed", f"{label}: {before or '?'} → {after or '?'}"))
    if old.network_rules is None or new.network_rules is None:
        if (old.network_rules is None) != (new.network_rules is None):
            out.append(("changed", "network rules: " + ("unknown" if new.network_rules is None
                                                        else "readable again")))  # fmt: skip
        return out
    was = {r.name: _rule(r) for r in old.network_rules}
    now = {r.name: _rule(r) for r in new.network_rules}
    out += [("added", f"rule {k}: {v}") for k, v in now.items() if k not in was]
    out += [("removed", f"rule {k}") for k in was if k not in now]
    out += [("changed", f"rule {k}: {was[k]} → {v}") for k, v in now.items()
            if k in was and was[k] != v]  # fmt: skip
    return out


def digest(sandbox: OpenShellSandbox) -> str:
    body = {k: v for k, v in to_dict(sandbox).items() if k != "observed_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class SandboxStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace(self, host_id: str, sandboxes: list[OpenShellSandbox], now: datetime) -> int:
        """Make ``sandboxes`` this host's full set: upsert them, delete the ones gone.

        Returns how many were created, changed or deleted.
        """
        changed = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT sandbox_id, snapshot, digest FROM openshell_sandboxes WHERE host_id = ?",
                (host_id,),
            ).fetchall()
            known = {r[0]: (r[1], r[2]) for r in rows}
            log: list[tuple[str, str, str, str, str]] = []
            for box in sandboxes:
                new_digest = digest(box)
                if box.sandbox_id in known and known[box.sandbox_id][1] == new_digest:
                    conn.execute(
                        "UPDATE openshell_sandboxes SET observed_at = ? WHERE sandbox_id = ?",
                        (ts(box.observed_at), box.sandbox_id),
                    )
                    continue
                if box.sandbox_id in known:
                    old = from_dict(OpenShellSandbox, json.loads(known[box.sandbox_id][0]))
                    changes = diff(old, box)
                    if len(changes) > MAX_CHANGES:
                        changes = [("changed", f"{len(changes)} changes")]
                elif self._owned_elsewhere(conn, box.sandbox_id, host_id):
                    continue  # a host may only report its own sandboxes
                else:
                    changes = [("created", f"in workspace {box.workspace}")]
                log += [(box.sandbox_id, box.name, ts(box.observed_at), c, d) for c, d in changes]
                conn.execute(
                    """
                    INSERT INTO openshell_sandboxes
                        (sandbox_id, host_id, name, observed_at, snapshot, digest)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (sandbox_id) DO UPDATE SET name = excluded.name,
                        observed_at = excluded.observed_at, snapshot = excluded.snapshot,
                        digest = excluded.digest
                    """,
                    (box.sandbox_id, host_id, box.name, ts(box.observed_at),
                     json.dumps(to_dict(box)), new_digest),
                )  # fmt: skip
                changed += 1
            gone = set(known) - {b.sandbox_id for b in sandboxes}
            for sandbox_id in gone:
                old = from_dict(OpenShellSandbox, json.loads(known[sandbox_id][0]))
                log.append((sandbox_id, old.name, ts(now), "deleted", ""))
                conn.execute("DELETE FROM openshell_sandboxes WHERE sandbox_id = ?", (sandbox_id,))
            changed += len(gone)
            conn.executemany(
                "INSERT INTO sandbox_changes (sandbox_id, host_id, name, at, change, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, host_id, name, at, c, d) for sid, name, at, c, d in log],
            )
        return changed

    @staticmethod
    def _owned_elsewhere(conn: sqlite3.Connection, sandbox_id: str, host_id: str) -> bool:
        row = conn.execute(
            "SELECT host_id FROM openshell_sandboxes WHERE sandbox_id = ?", (sandbox_id,)
        ).fetchone()
        return row is not None and row[0] != host_id

    def all(self, host_id: str | None = None) -> list[OpenShellSandbox]:
        """Sandboxes, by host then name."""
        where, args = ("WHERE host_id = ?", (host_id,)) if host_id else ("", ())
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT snapshot FROM openshell_sandboxes {where} ORDER BY host_id, name",  # noqa: S608
                args,
            ).fetchall()
        return [from_dict(OpenShellSandbox, json.loads(r[0])) for r in rows]

    def get(self, sandbox_id: str) -> OpenShellSandbox | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT snapshot FROM openshell_sandboxes WHERE sandbox_id = ?", (sandbox_id,)
            ).fetchone()
        return from_dict(OpenShellSandbox, json.loads(row[0])) if row else None

    def of_agent(self, agent_id: str, host_id: str) -> OpenShellSandbox | None:
        return next((s for s in self.all(host_id) if agent_id in s.agent_ids), None)

    def add_blocked(self, host_id: str, items: Sequence[BlockedRequest]) -> int:
        """Add to the blocked counts of this host's sandboxes; others' are ignored."""
        if not items:
            return 0
        with self.db.transaction() as conn:
            owned = conn.execute(
                "SELECT sandbox_id FROM openshell_sandboxes WHERE host_id = ?", (host_id,)
            )
            mine = {r[0] for r in owned}
            rows = [(b.sandbox_id, b.binary, b.host, b.port, b.count, ts(b.last_seen),
                     ts(b.last_seen), b.reason)
                    for b in items if b.sandbox_id in mine and b.host_id == host_id]  # fmt: skip
            conn.executemany(
                """
                INSERT INTO blocked_requests
                    (sandbox_id, binary, host, port, count, first_seen, last_seen, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (sandbox_id, binary, host, port) DO UPDATE SET
                    count = count + excluded.count,
                    last_seen = max(last_seen, excluded.last_seen),
                    reason = excluded.reason
                """,
                rows,
            )
        return len(rows)

    def blocked(self, sandbox_id: str, limit: int = 100) -> list[Blocked]:
        """What the sandbox blocked, most recent first."""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT sandbox_id, binary, host, port, count, first_seen, last_seen, reason "
                "FROM blocked_requests WHERE sandbox_id = ? ORDER BY last_seen DESC LIMIT ?",
                (sandbox_id, limit),
            ).fetchall()
        return [Blocked(r[0], r[1], r[2], r[3], r[4], required_ts(r[5]), required_ts(r[6]), r[7])
                for r in rows]  # fmt: skip

    def still_blocked(self, box: OpenShellSandbox, limit: int = 100) -> list[Blocked]:
        """What the sandbox blocked and its current rules still don't allow."""
        rules = box.network_rules or ()
        return [b for b in self.blocked(box.sandbox_id, limit)
                if not allows(rules, b.binary, b.host, b.port)]  # fmt: skip

    def changes(self, sandbox_id: str | None = None, limit: int = 50) -> list[SandboxChange]:
        where, args = ("WHERE sandbox_id = ?", (sandbox_id,)) if sandbox_id else ("", ())
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT at, sandbox_id, name, change, detail FROM sandbox_changes "  # noqa: S608
                f"{where} ORDER BY at DESC, id DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [SandboxChange(required_ts(r[0]), r[1], r[2], r[3], r[4]) for r in rows]
