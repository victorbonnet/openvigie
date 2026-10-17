"""OpenClaw: detection (FR-1) of each agent an OpenClaw gateway hosts.

One install per state dir (``~/.openclaw`` or ``$OPENCLAW_STATE_DIR``); each agent in
``agents.entries`` or ``agents/<id>/`` is one row, at most ``MAX_AGENTS``. They share the gateway
process, so ``processes()`` returns none: stopping one agent would stop all of them.

Version: the installed ``package.json`` next to the running gateway's ``dist/index.js``, else
``meta.lastTouchedVersion`` in ``openclaw.json``. Activity: newest mtime of the agent's session
files and session database, stat only. Usage: each agent's transcripts (``usage``). Only
``openclaw.json``, ``package.json`` and the agents' transcripts are read; credentials,
identity and auth files never are. A JSON5 ``openclaw.json`` still counts as an
install, with the version unknown and health ``degraded``.

Running (FR-2) means a gateway process (``openclaw gateway``, ``openclaw-gateway``): the CLI's
other commands (``tui``, ``status``) don't count. The state dir isn't on the gateway's command
line, so when several installs share the gateway's owner it can't be told which one it serves,
and none of them is marked running.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path, PurePath
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openvigie.adapters.base import ActivityBatch, Adapter, AdapterState, HostContext, ProcessInfo
from openvigie.adapters.common import candidate_roots, display_path, newest_mtime, owner_name
from openvigie.adapters.openclaw.usage import read_usage
from openvigie.model import AgentInstance, SandboxState, stable_id
from openvigie.safeio import UnsafeFileError, load_json_bounded

CONFIG = "openclaw.json"
MARKERS = (CONFIG, "agents")
NAMES = frozenset({"openclaw", "openclaw-gateway", "clawdbot", "moltbot"})
AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VERSION = r"^[0-9A-Za-z.+-]{1,64}$"
DEFAULT_AGENT = "main"
MAX_AGENTS = 100
MAX_AGENT_DIRS = 1000  # entries looked at under agents/, odd names included

Version = Annotated[str, Field(pattern=VERSION)]


class _ThirdParty(BaseModel):
    """Fields read from a file OpenClaw writes. Unknown fields are ignored: OpenClaw adds some
    between releases, and a new field must not hide the install."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)


class Meta(_ThirdParty):
    lastTouchedVersion: Version | None = None


class Agents(_ThirdParty):
    entries: dict[str, Any] = {}  # agent id → settings; ids are checked and capped when used


class OpenClawConfig(_ThirdParty):
    """The parts of ``openclaw.json`` detection uses."""

    meta: Meta = Meta()
    agents: Agents = Agents()


class Package(_ThirdParty):
    name: Annotated[str, Field(max_length=214)] | None = None
    version: Version | None = None


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


def is_gateway(proc: ProcessInfo) -> bool:
    """An OpenClaw gateway, not another command of the ``openclaw`` CLI."""
    if not is_openclaw_process(proc):
        return False
    return "openclaw-gateway" in (proc.name, PurePath(proc.exe).name) or (
        "gateway" in proc.cmdline[1:4]
    )


class OpenClawAdapter(Adapter):
    agent_type = "openclaw"
    display_name = "OpenClaw"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}
        self.dirs: dict[str, Path] = {}  # agent id → agents/<id>

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        self.roots, self.dirs = {}, {}
        roots = [
            r
            for r in candidate_roots(ctx, ".openclaw", "OPENCLAW_STATE_DIR")
            if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
        ]
        gateways = _assign([p for p in ctx.processes() if is_gateway(p)], roots)
        found = []
        for root in roots:
            config = self._typed(self._config(root))
            procs = gateways.get(root, [])
            version = _package_version(procs) or config.meta.lastTouchedVersion
            sandboxed, kind = (
                ctx.sandbox_of(procs[0].pid) if procs else (SandboxState.UNKNOWN, None)
            )
            for name in self._agent_ids(root, config):
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

    def _typed(self, raw: dict[str, Any] | None) -> OpenClawConfig:
        try:
            return OpenClawConfig.model_validate(raw or {})
        except ValidationError:
            self.notes.append(f"{CONFIG} has unexpected values: version and agents unknown")
            return OpenClawConfig()

    def _agent_ids(self, root: Path, config: OpenClawConfig) -> list[str]:
        """Agents in ``agents.entries`` and with a folder under ``agents/``, at most
        ``MAX_AGENTS``."""
        names = {k for k in config.agents.entries if AGENT_ID.match(k)}
        try:
            with os.scandir(root / "agents") as entries:
                for i, entry in enumerate(entries):
                    if i >= MAX_AGENT_DIRS:
                        break
                    if entry.is_dir(follow_symlinks=False) and AGENT_ID.match(entry.name):
                        names.add(entry.name)
        except OSError:
            pass
        valid = sorted(names)
        if len(valid) > MAX_AGENTS:
            self.notes.append(f"more than {MAX_AGENTS} agents: only the first {MAX_AGENTS} read")
        return valid[:MAX_AGENTS] or [DEFAULT_AGENT]


def _assign(gateways: list[ProcessInfo], roots: list[Path]) -> dict[Path, list[ProcessInfo]]:
    """Gateway processes per install: each goes to the one install its owner has, or to none
    when its owner has several."""
    owners = {root: owner_name(root) for root in roots}
    out: dict[Path, list[ProcessInfo]] = {}
    for proc in gateways:
        mine = roots if len(roots) == 1 else [r for r in roots if owners[r] == proc.username]
        if len(mine) == 1:
            out.setdefault(mine[0], []).append(proc)
    return out


def _activity(agent_dir: Path) -> datetime | None:
    found = [
        newest_mtime(agent_dir / "sessions", "*", max_depth=0),
        newest_mtime(agent_dir / "agent", "openclaw-agent.sqlite*", max_depth=0),
    ]
    times = [f[0] for f in found if f]
    return max(times) if times else None


def _package_version(procs: Sequence[ProcessInfo]) -> str | None:
    """``version`` of the ``openclaw`` package whose ``dist/`` script the gateway runs."""
    for proc in procs:
        for arg in proc.cmdline[1:3]:
            script = openclaw_script(arg)
            if script is None or script.parent.name != "dist":
                continue
            try:
                data = load_json_bounded(Path(script.parent.parent, "package.json"), 64 * 1024)
                package = Package.model_validate(data)
            except (OSError, ValueError, UnsafeFileError):  # ValidationError is a ValueError
                continue
            if package.name == "openclaw" and package.version:
                return package.version
    return None
