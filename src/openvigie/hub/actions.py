"""Write actions (FR-17, D14): propose, show the diff, confirm by typing the target's name,
then queue a command for the collector. Every step is audited.

Offers ``Kind`` and ``Plan`` (what an action does), ``KINDS`` (the actions that exist),
``ActionStore`` and the steps ``propose``, ``confirm``, ``cancel`` and ``rollback``. The routes
are in ``hub/admin``; each step there requires the admin role and a CSRF token.

A proposal stores the plan: the host, the name to type, the collector action and its arguments,
and the state before and after as text (the diff). It keeps ``before`` so the change can be
undone: ``rollback`` asks the kind for the inverse change and proposes it, to be confirmed like
any other. Only ``noop`` exists so far, to check the command channel end to end; it has nothing
to undo. Real actions (#17) add a ``Kind``.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from openvigie.hub.roles import Identity
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.store.db import Database, parse_ts, required_ts, ts
from openvigie.hub.store.inventory import Inventory

PROPOSAL_TTL = timedelta(minutes=15)
COMMAND_TTL = timedelta(minutes=10)


class ActionError(ValueError):
    """A step that can't be taken; the message is safe to show."""


@dataclass(frozen=True)
class Plan:
    host_id: str
    confirm_name: str  # the admin types this to confirm, e.g. the hostname or sandbox name
    command: str  # the collector action to run
    args: dict[str, str]
    summary: str
    before: str  # the current state, as text
    after: str  # the state after the change, as text


@dataclass(frozen=True)
class Kind:
    # the plan for these parameters; raises ActionError when they don't make sense
    plan: Callable[[Database, Mapping[str, str]], Plan]
    # the parameters of the change that undoes a confirmed one; None: nothing to undo
    undo: Callable[[StoredAction], dict[str, str] | None] = field(default=lambda action: None)


def _noop_plan(db: Database, params: Mapping[str, str]) -> Plan:
    row = Inventory(db).host(params.get("host_id", ""))
    if row is None:
        raise ActionError("unknown host")
    name = row.host.hostname
    return Plan(row.host.host_id, name, "noop", {}, f"Send a test command (noop) to {name}",
                before="", after=f"{name} runs noop and reports back\n")  # fmt: skip


KINDS: dict[str, Kind] = {"noop": Kind(_noop_plan)}


@dataclass(frozen=True)
class StoredAction:
    action_id: str
    kind: str
    plan: Plan
    rollback_of: str | None
    proposed_by: str
    proposed_at: datetime
    expires_at: datetime
    status: str  # proposed | confirmed | cancelled | expired
    decided_by: str | None
    decided_at: datetime | None
    command_id: str | None

    def current_status(self, now: datetime) -> str:
        return "expired" if self.status == "proposed" and now >= self.expires_at else self.status


COLUMNS = ("action_id, kind, host_id, confirm_name, command, args, summary, before, after, "
           "rollback_of, proposed_by, proposed_at, expires_at, status, decided_by, decided_at, "
           "command_id")  # fmt: skip


def _row(r: Any) -> StoredAction:
    plan = Plan(r[2], r[3], r[4], json.loads(r[5]), r[6], r[7], r[8])
    return StoredAction(r[0], r[1], plan, r[9], r[10], required_ts(r[11]), required_ts(r[12]),
                        r[13], r[14], parse_ts(r[15]), r[16])  # fmt: skip


class ActionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, kind: str, plan: Plan, who: Identity, now: datetime,
            rollback_of: str | None = None) -> StoredAction:  # fmt: skip
        action = StoredAction(secrets.token_hex(16), kind, plan, rollback_of, who.actor, now,
                              now + PROPOSAL_TTL, "proposed", None, None, None)  # fmt: skip
        with self.db.transaction() as conn:
            conn.execute(
                f"INSERT INTO actions ({COLUMNS}) VALUES ({', '.join('?' * 17)})",  # noqa: S608
                (action.action_id, kind, plan.host_id, plan.confirm_name, plan.command,
                 json.dumps(plan.args), plan.summary, plan.before, plan.after, rollback_of,
                 who.actor, ts(now), ts(action.expires_at), "proposed", None, None, None),
            )  # fmt: skip
        return action

    def get(self, action_id: str) -> StoredAction | None:
        with self.db.read() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM actions WHERE action_id = ?",  # noqa: S608
                (action_id,),
            ).fetchone()
        return _row(row) if row else None

    def recent(self, limit: int = 100) -> list[StoredAction]:
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM actions ORDER BY proposed_at DESC LIMIT ?",  # noqa: S608
                (limit,),
            ).fetchall()
        return [_row(r) for r in rows]

    def decide(self, action_id: str, status: str, who: Identity, now: datetime,
               command_id: str | None = None) -> bool:  # fmt: skip
        """Move a proposal that is still open to its final status; False if it wasn't open."""
        with self.db.transaction() as conn:
            return bool(conn.execute(
                "UPDATE actions SET status = ?, decided_by = ?, decided_at = ?, command_id = ? "
                "WHERE action_id = ? AND status = 'proposed' AND expires_at > ?",
                (status, who.actor, ts(now), command_id, action_id, ts(now)),
            ).rowcount)  # fmt: skip


# --- the steps -----------------------------------------------------------------------------


def _audit(db: Database, who: Identity, action: StoredAction, result: str, now: datetime,
           **values: Any) -> None:  # fmt: skip
    AuditLog(db).record(at=now, actor=who.actor, role=who.role, action=f"action.{action.kind}",
                        target=action.plan.confirm_name, result=result,
                        request_id=action.action_id, **values)  # fmt: skip


def propose(
    db: Database,
    kinds: Mapping[str, Kind],
    kind: str,
    params: Mapping[str, str],
    who: Identity,
    now: datetime,
    rollback_of: str | None = None,
) -> StoredAction:
    if kind not in kinds:
        raise ActionError("unknown action")
    plan = kinds[kind].plan(db, params)
    action = ActionStore(db).add(kind, plan, who, now, rollback_of)
    _audit(db, who, action, "proposed", now, before=plan.before, after=plan.after)
    return action


def confirm(db: Database, action: StoredAction, typed: str, who: Identity,
            now: datetime) -> StoredAction:  # fmt: skip
    """Queue the command, if ``typed`` is the target's name and the proposal is still open."""
    if action.current_status(now) != "proposed":
        raise ActionError(f"this proposal is {action.current_status(now)}")
    if typed.strip() != action.plan.confirm_name:
        _audit(db, who, action, "refused", now, after={"reason": "typed name did not match"})
        raise ActionError(f"type {action.plan.confirm_name} exactly to confirm")
    command_id = secrets.token_hex(16)
    # decide first: of two concurrent confirmations, only one queues a command
    if not ActionStore(db).decide(action.action_id, "confirmed", who, now, command_id):
        raise ActionError("this proposal was decided meanwhile")
    CommandQueue(db).enqueue(action.plan.host_id, action.plan.command, action.plan.args,
                             actor=who.actor, now=now, ttl=COMMAND_TTL,
                             command_id=command_id)  # fmt: skip
    _audit(db, who, action, "confirmed", now, before=action.plan.before,
           after={"command_id": command_id, "state": action.plan.after})  # fmt: skip
    return ActionStore(db).get(action.action_id) or action


def cancel(db: Database, action: StoredAction, who: Identity, now: datetime) -> None:
    if not ActionStore(db).decide(action.action_id, "cancelled", who, now):
        raise ActionError(f"this proposal is {action.current_status(now)}")
    _audit(db, who, action, "cancelled", now)


def rollback(db: Database, kinds: Mapping[str, Kind], action: StoredAction, who: Identity,
             now: datetime) -> StoredAction:  # fmt: skip
    """Propose the change that undoes a confirmed action."""
    kind = kinds.get(action.kind)
    params = kind.undo(action) if kind and action.status == "confirmed" else None
    if params is None:
        raise ActionError("nothing to roll back")
    return propose(db, kinds, action.kind, params, who, now, rollback_of=action.action_id)
