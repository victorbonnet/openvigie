"""OpenClaw token usage (FR-3) of one agent, from the three places its sessions end up.

Offers ``read_usage``:

* transcripts in the agent's database (``agent/openclaw-agent.sqlite``, ``transcript_events``),
  where OpenClaw 2026.9 keeps them, read by rowid;
* transcripts still in ``sessions/<id>.jsonl`` from before that move, tailed like other logs
  (``*.trajectory.jsonl`` holds run traces with prompts and per-run totals: never read);
* sessions run by the Codex harness, which keeps its own ``agent/codex-home``: read with the
  Codex adapter's rollout reader, since OpenClaw's transcripts carry no usage for them.

A transcript event is an assistant ``message`` with ``usage`` (``input``, ``output``,
``cacheRead``, ``cacheWrite``). ``input`` leaves cached tokens out for every provider; for
providers that count them inside (OpenAI), cache reads are added back so pricing sees each
provider's own convention. Record ids come from the session and event ids, so an event found
in both stores, or read again after OpenClaw rewrites a transcript, counts once at the hub.
Only usage, model and timestamps are kept: message content is never copied.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openvigie.adapters.base import ActivityBatch, AdapterState, Backlog, HostContext
from openvigie.adapters.codex import read_rollouts
from openvigie.adapters.usage import (
    TailBudget,
    before_backfill,
    int_or_none,
    parse_time,
    tail_files,
)
from openvigie.model import AgentInstance, Session, UsageQuality, UsageRecord, stable_id
from openvigie.safeio import UnsafeFileError, open_sqlite_readonly

DATABASE = Path("agent") / "openclaw-agent.sqlite"
CODEX_HOME = Path("agent") / "codex-home"
ROWS_PER_TICK = 2000
MAX_EVENT = 1024 * 1024
MAX_TEXT = 128
# rows without usage come back as NULL, so content is never even loaded
QUERY = """
    select rowid, session_id,
           case when instr(event_json, '"usage"') > 0 and length(event_json) <= ?
                then event_json end
    from transcript_events where rowid > ? order by rowid limit ?
"""
PROVIDERS = {"anthropic": "anthropic", "openai": "openai", "openai-codex": "openai"}
CACHED_INPUT_INCLUDED = {"openai"}


def read_usage(
    ctx: HostContext, agent: AgentInstance, agent_dir: Path, state: AdapterState
) -> tuple[ActivityBatch, list[str]]:
    """Everything new since the cursors in ``state``, and notes for health."""
    batch = ActivityBatch()
    notes: list[str] = []
    sessions: dict[str, Session] = {}
    for key, entry in tail_files(agent_dir, agent_dir / "sessions", "*.jsonl", state, batch,
                                 TailBudget.for_context(ctx), max_depth=0,
                                 exclude="*.trajectory.jsonl"):  # fmt: skip
        if before_backfill(entry, state):
            continue
        session = Path(key).name.removesuffix(".jsonl")
        _add(agent, session, entry, batch, sessions)
    backlog = batch.backlog
    db_notes, db_backlog = _read_database(agent, agent_dir / DATABASE, state, batch, sessions)
    notes += db_notes
    backlog = _merge(backlog, db_backlog)
    codex = agent_dir / CODEX_HOME
    if codex.is_dir() and not codex.is_symlink():
        harness = read_rollouts(ctx, agent, codex, "openai", state)
        batch.usage += harness.usage
        batch.spans += harness.spans
        batch.stats.add(harness.stats)
        sessions.update({s.session_id: s for s in harness.sessions})
        backlog = _merge(backlog, harness.backlog)
    batch.backlog = backlog
    batch.sessions = list(sessions.values())
    if batch.usage:
        batch.last_activity_at = max(r.timestamp for r in batch.usage)
    return batch, notes


def provider_of(provider: str, model: str) -> str | None:
    if "/" in model:  # routed ("vendor/model"): not priced in the table
        return None
    if provider in PROVIDERS:
        return PROVIDERS[provider]
    if model.startswith(("gpt-", "o1", "o3", "o4", "codex-")):
        return "openai"
    if model.startswith("claude-"):
        return "anthropic"
    return None


def _read_database(
    agent: AgentInstance,
    path: Path,
    state: AdapterState,
    batch: ActivityBatch,
    sessions: dict[str, Session],
) -> tuple[list[str], Backlog | None]:
    if not path.exists():
        return [], Backlog()
    key = f"db:{agent.agent_id}"
    saved = state.get(key) or {}
    after = saved.get("rowid") if isinstance(saved.get("rowid"), int) else 0
    try:
        conn = open_sqlite_readonly(path)
    except (OSError, UnsafeFileError, sqlite3.Error):
        return [f"{DATABASE.name} could not be opened"], None
    try:
        rows = conn.execute(QUERY, (MAX_EVENT, after, ROWS_PER_TICK)).fetchall()
    except sqlite3.Error:
        return [f"{DATABASE.name} has no readable transcript_events table"], None
    finally:
        conn.close()
    for rowid, session, event in rows:
        after = max(after, rowid)
        if not isinstance(event, str) or not isinstance(session, str):
            continue
        try:
            entry = json.loads(event)
        except ValueError:
            batch.stats.parse_errors += 1
            continue
        if not before_backfill(entry, state):
            _add(agent, session[:MAX_TEXT], entry, batch, sessions)
    state.stage(key, {"rowid": after})
    # more rows wait when the page was full: how many is unknown
    return [], Backlog(truncated=len(rows) == ROWS_PER_TICK)


def _add(
    agent: AgentInstance,
    session: str,
    entry: Any,
    batch: ActivityBatch,
    sessions: dict[str, Session],
) -> None:
    record = _record(agent, session, entry)
    if record is None:
        return
    batch.usage.append(record)
    model = record.response_model
    current = sessions.get(session)
    if current is None:
        sessions[session] = Session(session, agent.agent_id, record.timestamp, record.timestamp,
                                    (model,) if model else ())  # fmt: skip
        return
    models = current.models if not model or model in current.models else (*current.models, model)
    sessions[session] = Session(
        session,
        agent.agent_id,
        min(current.started_at or record.timestamp, record.timestamp),
        max(current.last_event_at or record.timestamp, record.timestamp),
        models,
    )


def _record(agent: AgentInstance, session: str, entry: Any) -> UsageRecord | None:
    if not isinstance(entry, dict) or entry.get("type") != "message":
        return None
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    event_id = entry.get("id")
    when = parse_time(entry.get("timestamp")) or _millis(message.get("timestamp"))
    if not isinstance(usage, dict) or not isinstance(event_id, str) or when is None:
        return None
    inp, out = int_or_none(usage.get("input")), int_or_none(usage.get("output"))
    cache_read, cache_write = (
        int_or_none(usage.get("cacheRead")),
        int_or_none(usage.get("cacheWrite")),
    )
    if not any((inp, out, cache_read, cache_write)):
        return None  # failed calls are logged with zero usage
    model = _text(message.get("model"))
    vendor = provider_of(_text(message.get("provider")), model)
    if vendor in CACHED_INPUT_INCLUDED and inp is not None:
        inp += cache_read or 0
    return UsageRecord(
        record_id=stable_id(agent.agent_id, session, event_id[:MAX_TEXT]),
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
        quality=UsageQuality.EXACT if inp is not None and out is not None else UsageQuality.UNKNOWN,
    )


def _text(value: Any) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _millis(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _merge(a: Backlog | None, b: Backlog | None) -> Backlog | None:
    """Two sources' backlogs as one; ``None`` (can't tell) wins."""
    if a is None or b is None:
        return None
    merged = Backlog(a.bytes_left + b.bytes_left, a.oldest, a.truncated or b.truncated)
    if b.oldest is not None:
        merged.oldest = b.oldest if a.oldest is None else min(a.oldest, b.oldest)
    return merged
