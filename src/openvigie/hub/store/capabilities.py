"""Capability snapshots per agent, and the history of what changed (FR-8)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from openvigie.hub.store.db import Database, parse_ts, ts
from openvigie.hub.store.usage import _owned_agents
from openvigie.model import Capabilities, from_dict, to_dict

SCALARS = ("approvals", "approval_mode", "filesystem", "network", "shell")
LISTS = ("writable_roots", "network_allowlist", "tools_allowed", "tools_denied", "skills")
MAX_CHANGES = 200  # per snapshot: a wholesale rewrite is summarised, not logged line by line


@dataclass(frozen=True)
class Change:
    at: datetime
    field: str
    change: str  # added | removed | changed
    detail: str


def _value(value: Any) -> str:
    return "unknown" if value is None else str(getattr(value, "value", value))


def _keyed(caps: Capabilities) -> dict[str, dict[str, str]]:
    """Named items per field, each rendered to a comparable string."""
    return {
        "mcp_servers": {
            s.name: f"{s.transport} {s.command or s.url_host or ''}"
            f"{' (unpinned)' if s.pinned is False else ''}"
            for s in caps.mcp_servers
        },
        "hooks": {f"{h.event}: {h.command or '?'}": "" for h in caps.hooks},
        "secrets": {f"{s.name} ({s.where})": "" for s in caps.secrets},
        **{name: dict.fromkeys(getattr(caps, name), "") for name in LISTS},
    }


def diff(old: Capabilities, new: Capabilities) -> list[tuple[str, str, str]]:
    """(field, change, detail) for every difference between two snapshots."""
    out: list[tuple[str, str, str]] = []
    for name in SCALARS:
        before, after = getattr(old, name), getattr(new, name)
        if before != after:
            out.append((name, "changed", f"{_value(before)} → {_value(after)}"))
    old_items, new_items = _keyed(old), _keyed(new)
    for name, now in new_items.items():
        was = old_items[name]
        out += [(name, "added", f"{k} {v}".strip()) for k, v in now.items() if k not in was]
        out += [(name, "removed", k) for k in was if k not in now]
        out += [(name, "changed", f"{k}: {was[k]} → {v}") for k, v in now.items()
                if k in was and was[k] != v]  # fmt: skip
    return out


def digest(caps: Capabilities) -> str:
    body = {k: v for k, v in to_dict(caps).items() if k != "observed_at"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class CapabilityStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def put(self, snapshots: list[Capabilities], host_id: str) -> int:
        """Store this host's snapshots; returns how many differed from the previous one."""
        changed = 0
        with self.db.transaction() as conn:
            owned = _owned_agents(conn, host_id)
            for caps in snapshots:
                if caps.agent_id not in owned:
                    continue
                new_digest = digest(caps)
                row = conn.execute(
                    "SELECT snapshot, digest FROM agent_capabilities WHERE agent_id = ?",
                    (caps.agent_id,),
                ).fetchone()
                if row and row[1] == new_digest:
                    conn.execute("UPDATE agent_capabilities SET observed_at = ? WHERE agent_id = ?",
                                 (ts(caps.observed_at), caps.agent_id))  # fmt: skip
                    continue
                if row:
                    changes = diff(from_dict(Capabilities, json.loads(row[0])), caps)
                    if len(changes) > MAX_CHANGES:
                        changes = [("all", "changed", f"{len(changes)} changes")]
                    conn.executemany(
                        "INSERT INTO capability_changes (agent_id, at, field, change, detail) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [(caps.agent_id, ts(caps.observed_at), *c) for c in changes],
                    )
                conn.execute(
                    """
                    INSERT INTO agent_capabilities (agent_id, observed_at, snapshot, digest)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (agent_id) DO UPDATE SET observed_at = excluded.observed_at,
                        snapshot = excluded.snapshot, digest = excluded.digest
                    """,
                    (caps.agent_id, ts(caps.observed_at), json.dumps(to_dict(caps)), new_digest),
                )
                changed += 1
        return changed

    def get(self, agent_id: str) -> Capabilities | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT snapshot FROM agent_capabilities WHERE agent_id = ?",
                               (agent_id,)).fetchone()  # fmt: skip
        return from_dict(Capabilities, json.loads(row[0])) if row else None

    def changes(self, agent_id: str, limit: int = 50) -> list[Change]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT at, field, change, detail FROM capability_changes WHERE agent_id = ? "
                "ORDER BY at DESC, id DESC LIMIT ?",
                (agent_id, limit),
            ).fetchall()
        return [Change(parse_ts(r[0]) or datetime.min, r[1], r[2], r[3]) for r in rows]
