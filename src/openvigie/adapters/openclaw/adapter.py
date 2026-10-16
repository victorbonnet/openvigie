"""OpenClaw: detection (FR-1) of each agent an OpenClaw gateway hosts.

One install per state dir (``~/.openclaw`` or ``$OPENCLAW_STATE_DIR``); each agent in
``agents.entries`` or ``agents/<id>/`` is one row. They share the gateway process, so
``processes()`` returns none: stopping one agent would stop all of them.

Version: the installed ``package.json`` next to the running gateway's ``dist/index.js``, else
``meta.lastTouchedVersion`` in ``openclaw.json``. Activity: newest mtime of the agent's session
files and session database, stat only. Usage: each agent's transcripts (``usage``). Only
``openclaw.json``, ``package.json`` and the agents' transcripts are read; credentials, identity
and auth files never are. A JSON5 ``openclaw.json`` still counts as an install, with the
version unknown and health ``degraded``.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any

from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext, ProcessInfo
from openvigie.adapters.common import (
    attribute_processes,
    candidate_roots,
    display_path,
    newest_mtime,
)
from openvigie.adapters.openclaw.usage import read_usage
from openvigie.model import AgentInstance, SandboxState, stable_id
from openvigie.safeio import UnsafeFileError, load_json_bounded

CONFIG = "openclaw.json"
MARKERS = (CONFIG, "agents")
NAMES = frozenset({"openclaw", "openclaw-gateway", "clawdbot", "moltbot"})
AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")
DEFAULT_AGENT = "main"


def openclaw_script(arg: str) -> PurePath | None:
    """The gateway's entry script (``…/openclaw/dist/index.js``) or ``openclaw`` launcher."""
    path = PurePath(arg)
    if path.name in NAMES or ("openclaw" in path.parts and path.parent.name == "dist"):
        return path
    return None


def is_openclaw_process(proc: ProcessInfo) -> bool:
    # node renames the gateway's main thread, so its name is often "MainThread"
    exe = PurePath(proc.exe).name
    if proc.name in NAMES or exe in NAMES:
        return True
    if exe in ("node", "bun") or proc.name in ("node", "bun"):
        return any(openclaw_script(arg) for arg in proc.cmdline[1:3])
    return False


class OpenClawAdapter(Adapter):
    agent_type = "openclaw"
    display_name = "OpenClaw"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}
        self.dirs: dict[str, Path] = {}  # agent id → agents/<id>

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        roots = [
            r
            for r in candidate_roots(ctx, ".openclaw", "OPENCLAW_STATE_DIR")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        gateway = [p for p in ctx.processes() if is_openclaw_process(p)]
        found = []
        for root in roots:
            config = self._config(root)
            procs = attribute_processes(gateway, root, roots)
            version = _package_version(procs) or _config_version(config)
            sandboxed, kind = (
                ctx.sandbox_of(procs[0].pid) if procs else (SandboxState.UNKNOWN, None)
            )
            for name in _agent_ids(root, config):
                agent_id = stable_id(ctx.host.host_id, self.agent_type, str(root), name)
                self.roots[agent_id] = root
                self.dirs[agent_id] = root / "agents" / name
                found.append(
                    AgentInstance(
                        agent_id=agent_id,
                        host_id=ctx.host.host_id,
                        agent_type=self.agent_type,
                        name=f"{self.display_name} · {name}",
                        version=version,
                        config_root=display_path(root / "agents" / name, ctx.homes),
                        sandboxed=sandboxed,
                        sandbox_kind=kind,
                        process_running=bool(procs),
                        last_activity_at=_activity(root / "agents" / name),
                    )
                )
        return found

    def processes(self, agent_id: str) -> Sequence[ProcessInfo]:
        return ()

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        agent_dir = self.dirs.get(agent.agent_id)
        if agent_dir is None:
            return ActivityBatch()
        batch, notes = read_usage(ctx, agent, agent_dir, state)
        self.notes += notes
        return batch

    def _config(self, root: Path) -> dict[str, Any] | None:
        path = root / CONFIG
        if not path.exists():
            return None
        try:
            data = load_json_bounded(path, max_bytes=1024 * 1024)
        except ValueError:
            self.notes.append(f"{CONFIG} is not strict JSON (JSON5?): version unknown")
            return None
        except (OSError, UnsafeFileError):
            self.notes.append(f"{CONFIG} could not be read")
            return None
        return data if isinstance(data, dict) else None


def _agent_ids(root: Path, config: dict[str, Any] | None) -> list[str]:
    """Agents configured in ``agents.entries`` and those with a folder under ``agents/``."""
    names: set[str] = set()
    agents = config.get("agents") if config else None
    entries = agents.get("entries") if isinstance(agents, dict) else None
    if isinstance(entries, dict):
        names |= {k for k in entries if isinstance(k, str)}
    with contextlib.suppress(OSError):
        names |= {p.name for p in (root / "agents").iterdir() if p.is_dir() and not p.is_symlink()}
    valid = sorted(n for n in names if AGENT_ID.match(n))
    return valid or [DEFAULT_AGENT]


def _activity(agent_dir: Path) -> datetime | None:
    found = [
        newest_mtime(agent_dir / "sessions", "*", max_depth=0),
        newest_mtime(agent_dir / "agent", "openclaw-agent.sqlite*", max_depth=0),
    ]
    times = [f[0] for f in found if f]
    return max(times) if times else None


def _config_version(config: dict[str, Any] | None) -> str | None:
    meta = config.get("meta") if config else None
    version = meta.get("lastTouchedVersion") if isinstance(meta, dict) else None
    return version if isinstance(version, str) and VERSION.match(version) else None


def _package_version(procs: Sequence[ProcessInfo]) -> str | None:
    """``version`` of the ``openclaw`` package whose ``dist/`` script the gateway runs."""
    for proc in procs:
        for arg in proc.cmdline[1:3]:
            script = openclaw_script(arg)
            if script is None or script.parent.name != "dist":
                continue
            try:
                data = load_json_bounded(Path(script.parent.parent, "package.json"), 64 * 1024)
            except (OSError, ValueError, UnsafeFileError):
                continue
            if isinstance(data, dict) and data.get("name") == "openclaw":
                version = data.get("version")
                if isinstance(version, str) and VERSION.match(version):
                    return version
    return None
