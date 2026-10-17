"""Hermes token usage (FR-3) from a profile's ``state.db``.

Offers ``read_usage``. ``session_model_usage`` keeps running totals per session, model, billing
route and task, updated in place while a session goes on. Each tick reads the rows whose
``last_seen`` moved since the cursor and reports how much each grew since the totals remembered
for it. A row met for the first time although it started before the cursor (a session resumed
after its totals were forgotten) only sets the baseline: missing one resume beats counting a
whole session twice. That holds only once a read got every row: while history spans several
pages, an unseen row is one not reached yet, and counts in full.

Hermes leaves cached tokens out of ``input_tokens`` for every provider. For providers that count
them inside (OpenAI), cache reads are added back so pricing sees each provider's own convention.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openvigie.adapters.base import ActivityBatch, AdapterState, Backlog
from openvigie.adapters.usage import int_or_none
from openvigie.model import AgentInstance, Session, UsageQuality, UsageRecord, stable_id
from openvigie.safeio import UnsafeFileError, open_sqlite_readonly

ROWS_PER_TICK = 2000
MAX_REMEMBERED = 20_000
FORGET_AFTER = timedelta(days=30)
MAX_TEXT = 128
QUERY = """
    select session_id, model, billing_provider, billing_base_url, billing_mode, task,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
           first_seen, last_seen
    from session_model_usage where last_seen >= ? order by last_seen limit ?
"""
# Hermes' billing providers → price table providers; the rest is guessed from the model name
PROVIDERS = {"anthropic": "anthropic", "openai": "openai", "openai-codex": "openai"}
CACHED_INPUT_INCLUDED = {"openai"}


@dataclass
class Cursor:
    seen: float | None = None  # last_seen of the newest row read
    rows: dict[str, list[float]] = field(default_factory=dict)  # row key → [*counts, last_seen]
    # whether the last read got every row; until then an unseen row is just not reached yet
    caught_up: bool = True

    @classmethod
    def load(cls, saved: dict[str, Any] | None) -> Cursor:
        if not saved:
            return cls()
        seen = saved.get("seen")
        rows = saved.get("rows")
        return cls(
            seen if isinstance(seen, int | float) else None,
            dict(rows) if isinstance(rows, dict) else {},
            saved.get("caught_up") is not False,
        )

    def dump(self) -> dict[str, Any]:
        if self.seen is not None:
            horizon = self.seen - FORGET_AFTER.total_seconds()
            self.rows = {k: v for k, v in self.rows.items() if v[-1] >= horizon}
        if len(self.rows) > MAX_REMEMBERED:
            newest = sorted(self.rows.items(), key=lambda kv: kv[1][-1])[-MAX_REMEMBERED:]
            self.rows = dict(newest)
        return {"seen": self.seen, "rows": self.rows, "caught_up": self.caught_up}


def provider_of(billing_provider: str, model: str) -> str | None:
    if "/" in model:  # routed (openrouter, nous): "vendor/model" isn't priced in the table
        return None
    if billing_provider in PROVIDERS:
        return PROVIDERS[billing_provider]
    if model.startswith(("gpt-", "o1", "o3", "o4", "codex-")):
        return "openai"
    if model.startswith("claude-"):
        return "anthropic"
    return None


def read_usage(
    agent: AgentInstance, home: Path, state: AdapterState, batch: ActivityBatch
) -> list[str]:
    """Fill ``batch`` with usage since the cursor in ``state``; returns notes for health."""
    path = home / "state.db"
    if not path.exists():
        batch.backlog = Backlog()
        return []
    key = f"usage:{agent.agent_id}"
    cursor = Cursor.load(state.get(key))
    start = cursor.seen
    if start is None and state.backfill_since:
        start = state.backfill_since.timestamp()
    try:
        conn = open_sqlite_readonly(path)
    except (OSError, UnsafeFileError, sqlite3.Error):
        return ["state.db could not be opened"]
    try:
        rows = conn.execute(QUERY, (start or 0.0, ROWS_PER_TICK)).fetchall()
    except sqlite3.Error:
        return ["state.db has no readable usage table"]
    finally:
        conn.close()
    sessions: dict[str, Session] = {}
    previous_tick = cursor.seen if cursor.caught_up else None
    for row in rows:
        record = _record(agent, row, cursor, resumed_before=previous_tick)
        if record is None:
            continue
        batch.usage.append(record)
        _add_session(sessions, agent, record, row[-2])
    batch.sessions = list(sessions.values())
    if batch.usage:
        batch.last_activity_at = max(r.timestamp for r in batch.usage)
    backlog = Backlog()
    backlog.truncated = len(rows) == ROWS_PER_TICK  # more rows wait: how many is unknown
    batch.backlog = backlog
    cursor.caught_up = not backlog.truncated
    state.stage(key, cursor.dump())
    return []


def _record(
    agent: AgentInstance, row: tuple[Any, ...], cursor: Cursor, resumed_before: float | None
) -> UsageRecord | None:
    session, model, provider, base_url, mode, task = (_text(v) for v in row[:6])
    counts = [int_or_none(v) for v in row[6:11]]
    first_seen, last_seen = row[11], row[12]
    if not session or not isinstance(last_seen, int | float) or isinstance(last_seen, bool):
        return None
    cursor.seen = last_seen if cursor.seen is None else max(cursor.seen, last_seen)
    row_key = stable_id(session, model, provider, base_url, mode, task)[:16]
    previous = cursor.rows.get(row_key)
    cursor.rows[row_key] = [float(c or 0) for c in counts] + [float(last_seen)]
    if previous is None:
        started = first_seen if isinstance(first_seen, int | float) else last_seen
        if resumed_before is not None and started < resumed_before:
            return None  # totals forgotten: this only sets the baseline
        delta = counts
    else:
        delta = [None if c is None else c - int(p) for c, p in zip(counts, previous, strict=False)]
    if any(d is not None and d < 0 for d in delta) or not any(delta):
        return None  # totals went down (session rewound) or nothing new
    try:
        when = datetime.fromtimestamp(float(last_seen), UTC)
    except (OverflowError, OSError, ValueError):
        return None
    vendor = provider_of(provider, model)
    inp, out, cache_read, cache_write, reasoning = delta
    if vendor in CACHED_INPUT_INCLUDED and inp is not None:
        inp += cache_read or 0
    totals = ":".join(str(c) for c in counts)
    return UsageRecord(
        record_id=stable_id(agent.agent_id, session, row_key, totals),
        agent_id=agent.agent_id,
        timestamp=when,
        session_id=session,
        provider=vendor,
        request_model=model or None,
        response_model=model or None,
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        reasoning_tokens=reasoning,
        quality=UsageQuality.EXACT if inp is not None and out is not None else UsageQuality.UNKNOWN,
    )


def _text(value: Any) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _add_session(
    sessions: dict[str, Session], agent: AgentInstance, record: UsageRecord, first_seen: Any
) -> None:
    session_id = record.session_id or ""
    try:
        started = datetime.fromtimestamp(float(first_seen), UTC)
    except (TypeError, OverflowError, OSError, ValueError):
        started = record.timestamp
    model = record.response_model
    current = sessions.get(session_id)
    if current is None:
        sessions[session_id] = Session(session_id, agent.agent_id, started, record.timestamp,
                                       (model,) if model else ())  # fmt: skip
        return
    models = current.models if not model or model in current.models else (*current.models, model)
    sessions[session_id] = Session(
        session_id,
        agent.agent_id,
        min(current.started_at or started, started),
        max(current.last_event_at or record.timestamp, record.timestamp),
        models,
    )
