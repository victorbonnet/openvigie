"""Detect Codex CLI installs and their running sessions (FR-1).

One agent per ``CODEX_HOME`` (default ``~/.codex``). Version and activity come from session
rollouts (``sessions/YYYY/MM/DD/rollout-*.jsonl``). The binary is never executed and
``auth.json`` is never opened.
"""

from __future__ import annotations

import tomllib
from pathlib import Path, PurePath

from openvigie.adapters.base import Adapter, HostContext, ProcessInfo
from openvigie.adapters.common import (
    attribute_processes,
    candidate_roots,
    display_path,
    newest_mtime,
)
from openvigie.model import AgentInstance, SandboxState, stable_id
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


class CodexAdapter(Adapter):
    agent_type = "codex"
    display_name = "Codex"

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".codex", "CODEX_HOME")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        codex_procs = [p for p in ctx.processes() if is_codex_process(p)]
        found = []
        for root in roots:
            self._check_config(root)
            procs = attribute_processes(codex_procs, root, roots)
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

    def _check_config(self, root: Path) -> None:
        config = root / "config.toml"
        if not config.exists():
            return
        try:
            load_toml_bounded(config, max_bytes=256 * 1024)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            self.notes.append("config.toml is not valid TOML")
        except OSError:
            self.notes.append("config.toml could not be read")

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
