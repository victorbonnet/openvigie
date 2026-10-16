"""Hermes Agent: detection (FR-1, FR-2), one agent per profile of each ``HERMES_HOME`` (default
``~/.hermes``): the home itself is the ``default`` profile, ``profiles/<name>/`` the others, each
with its own config, sessions and ``state.db``.

One gateway usually serves every profile (``served_profiles`` in its ``gateway_state.json``); a
profile can also run its own (``hermes -p <name> gateway``). The shared gateway's processes belong
to the default profile only: stopping a served profile from the hub would stop all of them. A
profile's own ``gateway_state.json`` is left over from before gateways were merged: ignored.

The version comes from the home's ``gateway_state.json``: the install's packaging files carry
placeholder versions. Activity is the newest message in each ``state.db``; ``gateway_state.json``
is rewritten as a heartbeat, so its mtime isn't activity. The gateway listens on no port:
processes are matched by running from the install. ``.env*``, ``auth.json`` and ``auth/`` are
never opened.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
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
PROFILE_MARKERS = ("config.yaml", "state.db")
PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_PROFILES = 100
MAX_VERSION = 64
DEFAULT = "default"


@dataclass(frozen=True)
class Gateway:
    """What a home's ``gateway_state.json`` says; empty when it can't be read."""

    version: str | None = None
    served: frozenset[str] = field(default_factory=frozenset)


def is_hermes_process(proc: ProcessInfo, roots: Sequence[Path]) -> bool:
    if under_root(proc, roots) is not None:
        return True
    python = PurePath(proc.exe).name.startswith("python")
    return proc.name == "hermes" and (python or any("hermes_cli" in a for a in proc.cmdline[:4]))


def profile_arg(proc: ProcessInfo) -> str | None:
    """The profile named on the command line (``-p <name>``, ``--profile[=]<name>``), if any."""
    args = proc.cmdline[:16]
    for i, arg in enumerate(args):
        if arg.startswith("--profile="):
            return arg.partition("=")[2] or None
        if arg in ("-p", "--profile") and i + 1 < len(args):
            return args[i + 1]
    return None


def profiles(root: Path) -> list[str]:
    """Named profiles under ``root/profiles``, sorted; symlinks and odd names are skipped."""
    names: list[str] = []
    with contextlib.suppress(OSError):
        for entry in sorted((root / "profiles").iterdir())[:MAX_PROFILES]:
            if (
                PROFILE_NAME.match(entry.name)
                and entry.name != DEFAULT
                and entry.is_dir()
                and not entry.is_symlink()
                and any((entry / m).exists() for m in PROFILE_MARKERS)
            ):
                names.append(entry.name)
    return names


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
            gateway = self._gateway(root)
            procs = self._attribute(hermes_procs, root, roots)
            named = profiles(root)
            shared = [p for p in procs if profile_arg(p) in (None, DEFAULT)]
            for name in [DEFAULT, *named]:
                home = root if name == DEFAULT else root / "profiles" / name
                own = shared if name == DEFAULT else [p for p in procs if profile_arg(p) == name]
                running = bool(own) or (bool(shared) and name in gateway.served)
                found.append(self._agent(ctx, root, name, home, own or shared, running, gateway,
                                         labelled=bool(named)))  # fmt: skip
                self.procs[found[-1].agent_id] = own
        return found

    def _agent(
        self,
        ctx: HostContext,
        root: Path,
        name: str,
        home: Path,
        procs: list[ProcessInfo],
        running: bool,
        gateway: Gateway,
        *,
        labelled: bool,
    ) -> AgentInstance:
        # the default profile keeps the id it had before profiles were told apart
        key = (str(root),) if name == DEFAULT else (str(root), name)
        agent_id = stable_id(ctx.host.host_id, self.agent_type, *key)
        self.roots[agent_id] = home
        self._check_config(home, name)
        sandboxed, kind = SandboxState.UNKNOWN, None
        if running and procs:
            sandboxed, kind = ctx.sandbox_of(procs[0].pid)
        return AgentInstance(
            agent_id=agent_id,
            host_id=ctx.host.host_id,
            agent_type=self.agent_type,
            name=f"{self.display_name} · {name}" if labelled else self.display_name,
            version=gateway.version,
            config_root=display_path(home, ctx.homes),
            sandboxed=sandboxed,
            sandbox_kind=kind,
            process_running=running,
            last_activity_at=self._last_message(home, name),
        )

    def processes(self, agent_id: str) -> Sequence[ProcessInfo]:
        return tuple(self.procs.get(agent_id, ()))

    @staticmethod
    def _attribute(procs: list[ProcessInfo], root: Path, roots: list[Path]) -> list[ProcessInfo]:
        """Path-matched processes go to their root; the others by owner, as for other agents."""
        mine = [p for p in procs if under_root(p, roots) == root]
        loose = [p for p in procs if under_root(p, roots) is None]
        return mine + attribute_processes(loose, root, roots)

    def _note(self, profile: str, message: str) -> None:
        self.notes.append(message if profile == DEFAULT else f"profile {profile}: {message}")

    def _check_config(self, home: Path, profile: str) -> None:
        config = home / "config.yaml"
        if not config.exists():
            return
        try:
            data = yaml.safe_load(read_text_bounded(config, max_bytes=256 * 1024))
        except (yaml.YAMLError, UnicodeDecodeError):
            self._note(profile, "config.yaml is not valid YAML")
            return
        except (OSError, UnsafeFileError):
            self._note(profile, "config.yaml could not be read")
            return
        if data is not None and not isinstance(data, dict):
            self._note(profile, "config.yaml is not a mapping")

    def _gateway(self, root: Path) -> Gateway:
        path = root / "gateway_state.json"
        if not path.exists():
            return Gateway()
        try:
            data = load_json_bounded(path, max_bytes=256 * 1024)
        except (OSError, UnsafeFileError, ValueError):
            self.notes.append("gateway_state.json could not be read")
            return Gateway()
        if not isinstance(data, dict):
            return Gateway()
        version = data.get("code_version")
        if not (isinstance(version, str) and 0 < len(version) <= MAX_VERSION):
            version = None
        served = data.get("served_profiles")
        if not isinstance(served, list):  # older gateways serve only their own home
            return Gateway(version, frozenset({DEFAULT}))
        names = (p for p in served[:MAX_PROFILES] if isinstance(p, str) and PROFILE_NAME.match(p))
        return Gateway(version, frozenset(names))

    def _last_message(self, home: Path, profile: str) -> datetime | None:
        path = home / "state.db"
        if not path.exists():
            return None
        try:
            conn = open_sqlite_readonly(path)
        except (OSError, UnsafeFileError, sqlite3.Error):
            self._note(profile, "state.db could not be opened")
            return None
        try:
            row = conn.execute(
                "select timestamp from messages order by rowid desc limit 1"
            ).fetchone()
        except sqlite3.Error:
            self._note(profile, "state.db has no readable messages table")
            return None
        finally:
            conn.close()
        if row is None or not isinstance(row[0], int | float) or isinstance(row[0], bool):
            return None
        try:
            return datetime.fromtimestamp(float(row[0]), UTC)
        except (OverflowError, OSError, ValueError):
            return None
