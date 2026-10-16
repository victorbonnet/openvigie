"""Codex CLI: detection (FR-1) and token usage from session rollouts (FR-3).

One agent per ``CODEX_HOME`` (default ``~/.codex``). Version, activity and usage come from
session rollouts (``sessions/YYYY/MM/DD/rollout-*.jsonl``), read incrementally. The binary is
never executed and ``auth.json`` is never opened.

Usage semantics (OpenAI): ``token_count`` events carry a cumulative ``total_token_usage``.
Records are the *deltas* between successive totals, so repeated events never double count.
``cached_input_tokens`` and ``cache_write_input_tokens`` are subsets of ``input_tokens`` (the
Responses API's ``input_tokens_details``), and ``reasoning_output_tokens`` a subset of
``output_tokens``; they are stored as such and pricing (#47) accounts for it. Rollouts written
before Codex recorded cache writes lack that field, which is then left unset.
``token_count`` events with ``info: null`` precede the first call and carry no usage.
When traces are enabled the same pass builds GenAI spans (``spans.py``, FR-6).
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePath
from typing import Any

from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext, ProcessInfo
from openvigie.adapters.codex.capabilities import read_capabilities
from openvigie.adapters.codex.spans import RolloutSpans
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
    Capabilities,
    SandboxState,
    Session,
    UsageQuality,
    UsageRecord,
    stable_id,
)
from openvigie.safeio import UnsafeFileError, iter_jsonl, load_toml_bounded

MARKERS = ("config.toml", "sessions", "history.jsonl", "auth.json", "version.json")
ROLLOUT_GLOB = "rollout-*.jsonl"


def is_codex_process(proc: ProcessInfo) -> bool:
    exe = PurePath(proc.exe).name
    if proc.name == "codex" or exe == "codex" or exe.startswith("codex-"):
        return True
    if proc.name in ("node", "bun") or exe in ("node", "bun"):
        return any("@openai/codex" in arg or arg.endswith("/codex") for arg in proc.cmdline[:4])
    return False


# record ids hash these cumulative totals; cache writes are left out so that ids of records read
# before they were mapped stay the same (a cache write always comes with more input_tokens)
ID_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
USAGE_FIELDS = (*ID_FIELDS, "cache_write_input_tokens")


class CodexAdapter(Adapter):
    agent_type = "codex"
    display_name = "Codex"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}
        self.providers: dict[str, str] = {}
        self.procs: dict[str, list[ProcessInfo]] = {}

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".codex", "CODEX_HOME")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        codex_procs = [p for p in ctx.processes() if is_codex_process(p)]
        found = []
        for root in roots:
            agent_id = stable_id(ctx.host.host_id, self.agent_type, str(root))
            self.roots[agent_id] = root
            self.providers[agent_id] = self._check_config(root)
            procs = attribute_processes(codex_procs, root, roots)
            self.procs[agent_id] = procs
            newest = newest_mtime(root / "sessions", ROLLOUT_GLOB)
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

    def processes(self, agent_id: str) -> Sequence[ProcessInfo]:
        return tuple(self.procs.get(agent_id, ()))

    def capabilities(self, ctx: HostContext, agent: AgentInstance) -> Capabilities | None:
        root = self.roots.get(agent.agent_id)
        if root is None:
            return None
        return read_capabilities(agent, root, ctx.homes, self.procs.get(agent.agent_id, []),
                                 ctx.now())  # fmt: skip

    def _check_config(self, root: Path) -> str:
        """Validate config.toml and return the model provider (default ``openai``)."""
        config = root / "config.toml"
        if not config.exists():
            return "openai"
        try:
            data = load_toml_bounded(config, max_bytes=256 * 1024)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            self.notes.append("config.toml is not valid TOML")
            return "openai"
        except OSError:
            self.notes.append("config.toml could not be read")
            return "openai"
        provider = data.get("model_provider")
        return provider if isinstance(provider, str) and 0 < len(provider) <= 64 else "openai"

    def _version(self, rollout: Path) -> str | None:
        try:
            for entry in iter_jsonl(rollout, max_lines=5, stats=self.stats):
                if isinstance(entry, dict) and entry.get("type") == "session_meta":
                    payload = entry.get("payload")
                    version = payload.get("cli_version") if isinstance(payload, dict) else None
                    if isinstance(version, str) and 0 < len(version) <= 64:
                        return version
        except (OSError, UnsafeFileError):
            self.notes.append("could not read newest rollout")
        return None

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        root = self.roots.get(agent.agent_id)
        batch = ActivityBatch()
        if root is None:
            return batch
        provider = self.providers.get(agent.agent_id, "openai")
        contexts: dict[str, dict[str, Any]] = {}
        sessions: dict[str, Session] = {}
        tracers: dict[str, RolloutSpans] = {}
        for key, entry in tail_files(
            root, root / "sessions", ROLLOUT_GLOB, state, batch, TailBudget.for_context(ctx)
        ):
            if key not in contexts:
                contexts[key] = dict(state.get("ctx:" + key) or {})
            record = self._entry(agent, provider, entry, contexts[key], state)
            if record is not None:
                batch.usage.append(record)
            if ctx.traces.enabled and not before_backfill(entry, state):
                if key not in tracers:
                    tracers[key] = RolloutSpans(
                        agent, key, state, provider=provider,
                        export_content=ctx.traces.export_content,
                    )  # fmt: skip
                tracers[key].add(entry, contexts[key].get("session"), record)
            session = _session(agent.agent_id, entry, contexts[key], sessions)
            if session is not None:
                sessions[session.session_id] = session
        for key, file_ctx in contexts.items():
            state.stage("ctx:" + key, file_ctx)
        batch.spans = [span for tracer in tracers.values() for span in tracer.finish()]
        batch.sessions = list(sessions.values())
        if batch.usage:
            batch.last_activity_at = max(r.timestamp for r in batch.usage)
        return batch

    def _entry(
        self,
        agent: AgentInstance,
        provider: str,
        entry: Any,
        file_ctx: dict[str, Any],
        state: AdapterState,
    ) -> UsageRecord | None:
        if not isinstance(entry, dict):
            return None
        kind, payload = entry.get("type"), entry.get("payload")
        if not isinstance(payload, dict):
            return None
        if kind == "session_meta" and isinstance(payload.get("id"), str):
            file_ctx["session"] = payload["id"][:128]
            return None
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            file_ctx["model"] = payload["model"][:128]
            return None
        if kind != "event_msg" or payload.get("type") != "token_count":
            return None
        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            return None
        current = {f: int_or_none(total.get(f)) for f in USAGE_FIELDS}
        previous = file_ctx.get("total") or dict.fromkeys(USAGE_FIELDS, 0)
        file_ctx["total"] = {f: v or 0 for f, v in current.items()}
        when = parse_time(entry.get("timestamp"))
        if when is None or (state.backfill_since and when < state.backfill_since):
            return None
        delta = {
            f: None if v is None else v - int(previous.get(f) or 0) for f, v in current.items()
        }
        if any(v is not None and v < 0 for v in delta.values()):  # counters reset: use last turn
            last = info.get("last_token_usage") if isinstance(info, dict) else None
            last = last if isinstance(last, dict) else {}
            delta = {f: int_or_none(last.get(f)) for f in USAGE_FIELDS}
        if all(not v for v in delta.values()):
            return None  # repeated event with the same totals
        session = file_ctx.get("session")
        model = file_ctx.get("model")
        known = delta["input_tokens"] is not None and delta["output_tokens"] is not None
        return UsageRecord(
            record_id=stable_id(
                agent.agent_id, str(session), *(str(current[f]) for f in ID_FIELDS)
            ),
            agent_id=agent.agent_id,
            timestamp=when,
            session_id=session,
            provider=provider,
            request_model=model,
            response_model=model,
            input_tokens=delta["input_tokens"],
            output_tokens=delta["output_tokens"],
            cache_read_input_tokens=delta["cached_input_tokens"],
            cache_creation_input_tokens=delta["cache_write_input_tokens"],
            reasoning_tokens=delta["reasoning_output_tokens"],
            quality=UsageQuality.EXACT if known else UsageQuality.UNKNOWN,
        )


def _session(
    agent_id: str, entry: Any, file_ctx: dict[str, Any], sessions: dict[str, Session]
) -> Session | None:
    session_id = file_ctx.get("session")
    when = parse_time(entry.get("timestamp")) if isinstance(entry, dict) else None
    if not isinstance(session_id, str) or when is None:
        return None
    model = file_ctx.get("model")
    current = sessions.get(session_id)
    if current is None:
        return Session(session_id, agent_id, when, when, (model,) if model else ())
    models = current.models if not model or model in current.models else (*current.models, model)
    return Session(
        session_id,
        agent_id,
        min(current.started_at or when, when),
        max(current.last_event_at or when, when),
        models,
    )
