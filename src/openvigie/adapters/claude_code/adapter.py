"""Detect Claude Code installs and their running sessions (FR-1).

One agent per config root: ``~/.claude`` for each configured home, plus ``$CLAUDE_CONFIG_DIR``.
Version and activity come from session transcripts (``projects/**/*.jsonl``); the ``claude``
binary is never executed and credential files are never opened.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from openvigie.adapters.base import Adapter, HostContext, ProcessInfo
from openvigie.adapters.common import (
    attribute_processes,
    candidate_roots,
    display_path,
    newest_mtime,
)
from openvigie.model import AgentInstance, SandboxState, stable_id
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

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".claude", "CLAUDE_CONFIG_DIR")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        claude_procs = [p for p in ctx.processes() if is_claude_process(p)]
        found = []
        for root in roots:
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
