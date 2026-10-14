"""Best-effort "is this agent sandboxed?" hint for the MVP inventory (FR-1).

Linux: a known launcher in the process ancestry (OpenShell, bwrap, firejail, container shims),
namespaces that differ from the collector's own, or a container cgroup mean "yes".
macOS: ``sandbox-exec`` in the ancestry means "yes"; anything else is "unknown", never "no",
because Seatbelt state isn't observable. Any error degrades to "unknown". Full OpenShell
inventory is P1 (FR-10).
"""

from __future__ import annotations

import fnmatch
import os
import platform
from collections.abc import Callable, Sequence
from pathlib import Path

from openvigie.adapters.base import ProcessInfo
from openvigie.model import SandboxState
from openvigie.safeio import read_text_bounded

LAUNCHERS: tuple[tuple[str, str], ...] = (
    ("openshell*", "openshell"),
    ("bwrap", "bwrap"),
    ("firejail", "firejail"),
    ("nsjail", "nsjail"),
    ("systemd-nspawn", "nspawn"),
    ("containerd-shim*", "container"),
    ("conmon", "container"),
    ("sandbox-exec", "seatbelt"),
)
NAMESPACES = ("mnt", "net", "user", "pid")
CONTAINER_CGROUP_HINTS = ("docker", "containerd", "kubepods", "libpod", "lxc")
MAX_DEPTH = 64

Result = tuple[SandboxState, str | None]


class SandboxProbe:
    def __init__(
        self,
        processes: Callable[[], Sequence[ProcessInfo]],
        *,
        system: str | None = None,
        proc_root: Path = Path("/proc"),
        self_pid: int | None = None,
        readlink: Callable[[Path], str] = os.readlink,
    ) -> None:
        self.processes = processes
        self.system = (system or platform.system()).lower()
        self.proc_root = proc_root
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        self.readlink = readlink

    def __call__(self, pid: int) -> Result:
        try:
            launcher = self._launcher(pid)
            if launcher:
                return SandboxState.YES, launcher
            if self.system != "linux":
                return SandboxState.UNKNOWN, None
            return self._linux(pid)
        except Exception:
            return SandboxState.UNKNOWN, None

    def _launcher(self, pid: int) -> str | None:
        by_pid = {p.pid: p for p in self.processes()}
        current = by_pid.get(pid)
        for _ in range(MAX_DEPTH):
            if current is None or current.ppid in (0, current.pid):
                return None
            current = by_pid.get(current.ppid)
            if current is None:
                return None
            for pattern, kind in LAUNCHERS:
                if fnmatch.fnmatchcase(current.name, pattern):
                    return kind
        return None

    def _linux(self, pid: int) -> Result:
        try:
            differs = [
                ns
                for ns in NAMESPACES
                if self.readlink(self.proc_root / str(pid) / "ns" / ns)
                != self.readlink(self.proc_root / str(self.self_pid) / "ns" / ns)
            ]
        except PermissionError:
            return SandboxState.UNKNOWN, None
        except FileNotFoundError:
            return SandboxState.UNKNOWN, None
        if any(ns in differs for ns in ("mnt", "net", "user")):
            return SandboxState.YES, "namespaces"
        try:
            cgroup = read_text_bounded(self.proc_root / str(pid) / "cgroup", 64 * 1024)
        except OSError:
            cgroup = ""
        if any(hint in cgroup for hint in CONTAINER_CGROUP_HINTS):
            return SandboxState.YES, "container"
        return SandboxState.NO, None
