"""Hermes Agent: detection (FR-1, FR-2), one agent per profile of each ``HERMES_HOME`` (default
``~/.hermes``): the home itself is the ``default`` profile, ``profiles/<name>/`` the others, each
with its own config, sessions and ``state.db``.

One gateway usually serves every profile (``served_profiles`` in its ``gateway_state.json``); a
profile can also run its own (``hermes -p <name> gateway run``). The shared gateway's processes
belong to the default profile only: stopping a served profile from the hub would stop all of
them. A served profile counts as running only while a default-profile gateway runs, not any
default-profile process (``hermes chat``, helpers). A profile's own ``gateway_state.json`` is left
over from before gateways were merged: ignored.

The version comes from the home's ``gateway_state.json``: the install's packaging files carry
placeholder versions. Activity is the newest message in each ``state.db``; ``gateway_state.json``
is rewritten as a heartbeat, so its mtime isn't activity. The gateway listens on no port:
processes are matched by running from the install, helpers by their parent. ``.env*``,
``auth.json`` and ``auth/`` are never opened.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path, PurePath
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openvigie.adapters.base import Adapter, HostContext, ProcessInfo
from openvigie.adapters.common import candidate_roots, display_path, owner_name
from openvigie.model import AgentInstance, SandboxState, stable_id
from openvigie.safeio import (
    UnsafeFileError,
    load_json_bounded,
    open_sqlite_readonly,
    read_text_bounded,
)

MARKERS = ("config.yaml", "state.db", "gateway_state.json", "hermes-agent")
PROFILE_MARKERS = ("config.yaml", "state.db")
PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
PROFILE_NAME = re.compile(PROFILE_PATTERN)
MAX_PROFILES = 100
MAX_PROFILE_ENTRIES = 1000
MAX_ANCESTORS = 16
DEFAULT = "default"

ProfileName = Annotated[str, Field(pattern=PROFILE_PATTERN)]


class GatewayState(BaseModel):
    """The fields of ``gateway_state.json`` openvigie reads. Unknown fields are ignored: the
    gateway writes this file and adds fields between releases."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore", hide_input_in_errors=True)

    code_version: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    served_profiles: Annotated[list[ProfileName], Field(max_length=MAX_PROFILES)] | None = None


@dataclass(frozen=True)
class Gateway:
    """What a home's ``gateway_state.json`` says; empty when it can't be read."""

    version: str | None = None
    served: frozenset[str] = field(default_factory=frozenset)


def is_loose_hermes(proc: ProcessInfo) -> bool:
    """A Hermes process that doesn't run from a known home: a pip or Homebrew install."""
    python = PurePath(proc.exe).name.lower().startswith("python")  # macOS framework: ``Python``
    return any("hermes_cli" in a for a in proc.cmdline[:4]) or (proc.name == "hermes" and python)


def is_gateway(proc: ProcessInfo) -> bool:
    args = proc.cmdline[:16]
    return any(a == "gateway" and b == "run" for a, b in pairwise(args))


def profile_arg(proc: ProcessInfo) -> str | None:
    """The profile named on the command line (``-p <name>``, ``--profile[=]<name>``), if any."""
    args = proc.cmdline[:16]
    for i, arg in enumerate(args):
        if arg.startswith("--profile="):
            return arg.partition("=")[2] or None
        if arg in ("-p", "--profile") and i + 1 < len(args):
            return args[i + 1]
    return None


def profile_of(proc: ProcessInfo, by_pid: Mapping[int, ProcessInfo]) -> str:
    """The profile named by the process or its nearest Hermes ancestor: a gateway's helpers carry
    no ``-p`` of their own."""
    seen: set[int] = set()
    current: ProcessInfo | None = proc
    while current is not None and current.pid not in seen and len(seen) < MAX_ANCESTORS:
        seen.add(current.pid)
        if (name := profile_arg(current)) is not None:
            return name
        current = by_pid.get(current.ppid)
    return DEFAULT


def profiles(root: Path) -> tuple[list[str], bool]:
    """Named profiles under ``root/profiles``, sorted, and whether the listing was cut short.
    Symlinks and odd names are skipped before counting toward ``MAX_PROFILES``."""
    names: list[str] = []
    truncated = False
    try:
        with os.scandir(root / "profiles") as it:
            for seen, entry in enumerate(it, 1):
                if seen > MAX_PROFILE_ENTRIES or len(names) == MAX_PROFILES:
                    truncated = True
                    break
                if (
                    PROFILE_NAME.match(entry.name)
                    and entry.name != DEFAULT
                    and entry.is_dir(follow_symlinks=False)
                    and any((Path(entry.path) / m).exists() for m in PROFILE_MARKERS)
                ):
                    names.append(entry.name)
    except OSError:
        pass
    return sorted(names), truncated


def hermes_roots(ctx: HostContext) -> list[Path]:
    """Homes with a Hermes install. A ``HERMES_HOME`` that is another home's profile is left to
    that home, so the profile isn't reported twice."""
    roots = [
        r
        for r in candidate_roots(ctx, ".hermes", "HERMES_HOME")
        if r.is_dir() and not r.is_symlink() and any((r / m).exists() for m in MARKERS)
    ]
    return [r for r in roots if not (r.parent.name == "profiles" and r.parent.parent in roots)]


def under_root(proc: ProcessInfo, prefixes: Mapping[Path, str]) -> Path | None:
    """The root whose files this process runs (its interpreter or its script); the deepest one
    when roots are nested."""
    paths = [proc.exe, *proc.cmdline[:2]]
    matches = [r for r, prefix in prefixes.items() if any(p.startswith(prefix) for p in paths)]
    return max(matches, key=lambda r: len(prefixes[r]), default=None)


def assign(procs: Iterable[ProcessInfo], roots: Sequence[Path]) -> dict[Path, list[ProcessInfo]]:
    """Each Hermes process goes to one root: the one it runs from, else the only root owned by
    its user. A loose process that could belong to several roots is left out."""
    # process paths are resolved, so the roots are too (``/var`` -> ``/private/var`` on macOS)
    prefixes = {r: str(r.resolve()).rstrip("/") + "/" for r in roots}
    owners = {r: owner_name(r) for r in roots}
    by_root: dict[Path, list[ProcessInfo]] = {r: [] for r in roots}
    for proc in procs:
        root = under_root(proc, prefixes)
        if root is None and is_loose_hermes(proc):
            mine = [r for r in roots if len(roots) == 1 or owners[r] == proc.username]
            root = mine[0] if len(mine) == 1 else None
        if root is not None:
            by_root[root].append(proc)
    return by_root


def group_by_profile(procs: list[ProcessInfo]) -> dict[str, list[ProcessInfo]]:
    by_pid = {p.pid: p for p in procs}
    groups: dict[str, list[ProcessInfo]] = {}
    for proc in procs:
        groups.setdefault(profile_of(proc, by_pid), []).append(proc)
    return groups


class HermesAdapter(Adapter):
    agent_type = "hermes"
    display_name = "Hermes"

    def __init__(self) -> None:
        super().__init__()
        self.roots: dict[str, Path] = {}
        self.procs: dict[str, list[ProcessInfo]] = {}
        self.configs: dict[Path, tuple[tuple[int, int], str | None]] = {}

    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        self.roots, self.procs = {}, {}
        roots = hermes_roots(ctx)
        by_root = assign(ctx.processes(), roots)
        found = []
        for root in roots:
            where = display_path(root, ctx.homes)
            gateway = self._gateway(root, where)
            named, truncated = profiles(root)
            if truncated:
                self.notes.append(f"{where}: more profiles than read, some are not shown")
            groups = group_by_profile(by_root[root])
            shared = [p for p in groups.get(DEFAULT, []) if is_gateway(p)]
            for name in [DEFAULT, *named]:
                home = root if name == DEFAULT else root / "profiles" / name
                own = groups.get(name, [])
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

    def _note(self, profile: str, message: str) -> None:
        self.notes.append(message if profile == DEFAULT else f"profile {profile}: {message}")

    def _check_config(self, home: Path, profile: str) -> None:
        config = home / "config.yaml"
        try:
            stat = config.stat()
        except OSError:
            return
        # parsed again only when the file changes: this runs every tick, on a Pi too
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self.configs.get(config)
        if cached is None or cached[0] != key:
            cached = (key, config_problem(config))
            self.configs[config] = cached
        if cached[1] is not None:
            self._note(profile, cached[1])

    def _gateway(self, root: Path, where: str) -> Gateway:
        path = root / "gateway_state.json"
        if not path.exists():
            return Gateway()
        try:
            state = GatewayState.model_validate(load_json_bounded(path, max_bytes=256 * 1024))
        except (OSError, UnsafeFileError, ValueError, ValidationError):
            self.notes.append(f"{where}/gateway_state.json could not be read")
            return Gateway()
        if state.served_profiles is None:  # older gateways serve only their own home
            return Gateway(state.code_version, frozenset({DEFAULT}))
        return Gateway(state.code_version, frozenset(state.served_profiles))

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


def config_problem(config: Path) -> str | None:
    try:
        data = yaml.safe_load(read_text_bounded(config, max_bytes=256 * 1024))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError):
        return "config.yaml is not valid YAML"
    except (OSError, UnsafeFileError):
        return "config.yaml could not be read"
    if data is not None and not isinstance(data, dict):
        return "config.yaml is not a mapping"
    return None
