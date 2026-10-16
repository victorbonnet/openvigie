"""Hermes Agent: detection (FR-1, FR-2), one agent per ``HERMES_HOME`` (default ``~/.hermes``).

The version comes from ``gateway_state.json``: the install's packaging files carry placeholder
versions. Activity is the newest message in ``state.db``; ``gateway_state.json`` is rewritten as
a heartbeat, so its mtime isn't activity. The gateway listens on no port: processes are matched
by running from the install. ``.env*``, ``auth.json`` and ``auth/`` are never opened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePath

import yaml

from openvigie.adapters.base import Adapter, HostContext, ProcessInfo
from openvigie.adapters.common import attribute_processes, candidate_roots, display_path
from openvigie.model import AgentInstance, SandboxState, stable_id
from openvigie.safeio import (
    UnsafeFileError,
    load_json_bounded,
    open_sqlite_readonly,
    read_text_bounded,
)

MARKERS = ("config.yaml", "state.db", "gateway_state.json", "hermes-agent")
MAX_VERSION = 64


def is_hermes_process(proc: ProcessInfo, roots: Sequence[Path]) -> bool:
    if under_root(proc, roots) is not None:
        return True
    python = PurePath(proc.exe).name.startswith("python")
    return proc.name == "hermes" and (python or any("hermes_cli" in a for a in proc.cmdline[:4]))


def under_root(proc: ProcessInfo, roots: Sequence[Path]) -> Path | None:
    """The root whose files this process runs (its interpreter or its script), if any."""
    paths = [proc.exe, *proc.cmdline[:2]]
    for root in roots:
        prefix = str(root).rstrip("/") + "/"
        if any(p.startswith(prefix) for p in paths):
            return root
    return None


class HermesAdapter(Adapter):
    agent_type = "hermes"
    display_name = "Hermes"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}
        self.procs: dict[str, list[ProcessInfo]] = {}

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".hermes", "HERMES_HOME")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        hermes_procs = [p for p in ctx.processes() if is_hermes_process(p, roots)]
        found = []
        for root in roots:
            agent_id = stable_id(ctx.host.host_id, self.agent_type, str(root))
            self.roots[agent_id] = root
            self._check_config(root)
            procs = self._attribute(hermes_procs, root, roots)
            self.procs[agent_id] = procs
            sandboxed, kind = SandboxState.UNKNOWN, None
            if procs:
                sandboxed, kind = ctx.sandbox_of(procs[0].pid)
            found.append(
                AgentInstance(
                    agent_id=agent_id,
                    host_id=ctx.host.host_id,
                    agent_type=self.agent_type,
                    name=self.display_name,
                    version=self._version(root),
                    config_root=display_path(root, ctx.homes),
                    sandboxed=sandboxed,
                    sandbox_kind=kind,
                    process_running=bool(procs),
                    last_activity_at=self._last_message(root),
                )
            )
        return found

    def processes(self, agent_id: str) -> Sequence[ProcessInfo]:
        return tuple(self.procs.get(agent_id, ()))

    @staticmethod
    def _attribute(procs: list[ProcessInfo], root: Path, roots: list[Path]) -> list[ProcessInfo]:
        """Path-matched processes go to their root; the others by owner, as for other agents."""
        mine = [p for p in procs if under_root(p, roots) == root]
        loose = [p for p in procs if under_root(p, roots) is None]
        return mine + attribute_processes(loose, root, roots)

    def _check_config(self, root: Path) -> None:
        config = root / "config.yaml"
        if not config.exists():
            return
        try:
            data = yaml.safe_load(read_text_bounded(config, max_bytes=256 * 1024))
        except (yaml.YAMLError, UnicodeDecodeError):
            self.notes.append("config.yaml is not valid YAML")
            return
        except (OSError, UnsafeFileError):
            self.notes.append("config.yaml could not be read")
            return
        if data is not None and not isinstance(data, dict):
            self.notes.append("config.yaml is not a mapping")

    def _version(self, root: Path) -> str | None:
        path = root / "gateway_state.json"
        if not path.exists():
            return None
        try:
            data = load_json_bounded(path, max_bytes=256 * 1024)
        except (OSError, UnsafeFileError, ValueError):
            self.notes.append("gateway_state.json could not be read")
            return None
        version = data.get("code_version") if isinstance(data, dict) else None
        if isinstance(version, str) and 0 < len(version) <= MAX_VERSION:
            return version
        return None

    def _last_message(self, root: Path) -> datetime | None:
        path = root / "state.db"
        if not path.exists():
            return None
        try:
            conn = open_sqlite_readonly(path)
        except (OSError, UnsafeFileError, sqlite3.Error):
            self.notes.append("state.db could not be opened")
            return None
        try:
            row = conn.execute(
                "select timestamp from messages order by rowid desc limit 1"
            ).fetchone()
        except sqlite3.Error:
            self.notes.append("state.db has no readable messages table")
            return None
        finally:
            conn.close()
        if row is None or not isinstance(row[0], int | float) or isinstance(row[0], bool):
            return None
        try:
            return datetime.fromtimestamp(float(row[0]), UTC)
        except (OverflowError, OSError, ValueError):
            return None
