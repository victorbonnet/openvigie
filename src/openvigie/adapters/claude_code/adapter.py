"""Claude Code: detection (FR-1) and token usage from session transcripts (FR-3).

One agent per config root: ``~/.claude`` for each configured home, plus ``$CLAUDE_CONFIG_DIR``.
Version, activity and usage come from session transcripts (``projects/**/*.jsonl``), read
incrementally. Only ids, timestamps, models and usage numbers are kept, never message content.
When traces are enabled the same pass builds GenAI spans (``spans.py``, FR-6); content goes
into them only with ``export_content``, redacted.
The ``claude`` binary is never executed and credential files are never opened.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path, PurePath

from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext, ProcessInfo
from openvigie.adapters.claude_code.spans import TranscriptSpans
from openvigie.adapters.common import (
    attribute_processes,
    candidate_roots,
    display_path,
    newest_mtime,
)
from openvigie.adapters.usage import (
    TailBudget,
    before_backfill,
    int_or_none,
    parse_time,
    tail_files,
)
from openvigie.model import (
    AgentInstance,
    SandboxState,
    Session,
    UsageQuality,
    UsageRecord,
    stable_id,
)
from openvigie.safeio import UnsafeFileError, iter_jsonl

MARKERS = ("projects", "settings.json", "history.jsonl", "sessions")
VERSION_SCAN_LINES = 50


def is_claude_process(proc: ProcessInfo) -> bool:
    exe = PurePath(proc.exe).name
    if proc.name == "claude" or exe == "claude":
        return True
    if proc.name in ("node", "bun") or exe in ("node", "bun"):
        return any("claude-code" in arg or arg.endswith("/claude") for arg in proc.cmdline[:4])
    return False


class ClaudeCodeAdapter(Adapter):
    agent_type = "claude-code"
    display_name = "Claude Code"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".claude", "CLAUDE_CONFIG_DIR")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        claude_procs = [p for p in ctx.processes() if is_claude_process(p)]
        found = []
        for root in roots:
            self.roots[stable_id(ctx.host.host_id, self.agent_type, str(root))] = root
            procs = attribute_processes(claude_procs, root, roots)
            newest = newest_mtime(root / "projects", "*.jsonl")
            sandboxed, kind = SandboxState.UNKNOWN, None
            if procs:
                sandboxed, kind = ctx.sandbox_of(procs[0].pid)
            found.append(
                AgentInstance(
                    agent_id=stable_id(ctx.host.host_id, self.agent_type, str(root)),
                    host_id=ctx.host.host_id,
                    agent_type=self.agent_type,
                    name=self.display_name,
                    version=self._version(newest[1]) if newest else None,
                    config_root=display_path(root, ctx.homes),
                    sandboxed=sandboxed,
                    sandbox_kind=kind,
                    process_running=bool(procs),
                    last_activity_at=newest[0] if newest else None,
                )
            )
        return found

    def _version(self, transcript: Path) -> str | None:
        try:
            for entry in iter_jsonl(transcript, max_lines=VERSION_SCAN_LINES, stats=self.stats):
                version = entry.get("version") if isinstance(entry, dict) else None
                if isinstance(version, str) and 0 < len(version) <= 64:
                    return version
        except (OSError, UnsafeFileError):
            self.notes.append("could not read newest transcript")
        return None

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        root = self.roots.get(agent.agent_id)
        batch = ActivityBatch()
        if root is None:
            return batch
        records: dict[str, UsageRecord] = {}
        sessions: dict[str, Session] = {}
        tracers: dict[str, TranscriptSpans] = {}
        files = tail_files(root, root / "projects", "*.jsonl", state, batch, TailBudget())
        for key, entry in files:
            record = self._usage(agent, entry, state)
            if ctx.traces.enabled and not before_backfill(entry, state):
                if key not in tracers:
                    tracers[key] = TranscriptSpans(
                        agent, key, state, export_content=ctx.traces.export_content
                    )
                tracers[key].add(entry, record)
            if record is None:
                continue
            records[record.record_id] = record  # one API response spans several lines
            if record.session_id:
                sessions[record.session_id] = _merge_session(
                    sessions.get(record.session_id), agent.agent_id, record
                )
        batch.spans = [span for tracer in tracers.values() for span in tracer.finish()]
        batch.usage = list(records.values())
        batch.sessions = list(sessions.values())
        if batch.usage:
            batch.last_activity_at = max(r.timestamp for r in batch.usage)
        return batch

    def _usage(
        self, agent: AgentInstance, entry: object, state: AdapterState
    ) -> UsageRecord | None:
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            return None
        message = entry.get("message")
        if not isinstance(message, dict):
            return None
        message_id, model = message.get("id"), message.get("model")
        when = parse_time(entry.get("timestamp"))
        if not isinstance(message_id, str) or when is None or model == SYNTHETIC_MODEL:
            return None
        if state.backfill_since and when < state.backfill_since:
            return None
        request_id = entry.get("requestId")
        session_id = entry.get("sessionId")
        usage = message.get("usage")
        record = UsageRecord(
            record_id=stable_id(agent.agent_id, message_id, str(request_id)),
            agent_id=agent.agent_id,
            timestamp=when,
            session_id=session_id if isinstance(session_id, str) else None,
            provider="anthropic",
            request_model=_short(entry.get("requestedModel")),
            response_model=_short(model),
        )
        if not isinstance(usage, dict):
            return record  # usage unknown, not zero
        details = usage.get("output_tokens_details")
        thinking = details.get("thinking_tokens") if isinstance(details, dict) else None
        creation = usage.get("cache_creation")
        one_hour = creation.get("ephemeral_1h_input_tokens") if isinstance(creation, dict) else None
        return dataclasses.replace(
            record,
            input_tokens=int_or_none(usage.get("input_tokens")),
            output_tokens=int_or_none(usage.get("output_tokens")),
            cache_read_input_tokens=int_or_none(usage.get("cache_read_input_tokens")),
            cache_creation_input_tokens=int_or_none(usage.get("cache_creation_input_tokens")),
            cache_creation_1h_input_tokens=int_or_none(one_hour),
            reasoning_tokens=int_or_none(thinking),
            quality=UsageQuality.EXACT,
        )


SYNTHETIC_MODEL = "<synthetic>"


def _short(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _merge_session(current: Session | None, agent_id: str, record: UsageRecord) -> Session:
    model = record.response_model or record.request_model
    if current is None:
        return Session(
            session_id=record.session_id or "",
            agent_id=agent_id,
            started_at=record.timestamp,
            last_event_at=record.timestamp,
            models=(model,) if model else (),
        )
    models = current.models if not model or model in current.models else (*current.models, model)
    return dataclasses.replace(
        current,
        started_at=min(current.started_at or record.timestamp, record.timestamp),
        last_event_at=max(current.last_event_at or record.timestamp, record.timestamp),
        models=models,
    )
