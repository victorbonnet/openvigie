"""Which agent runs in which OpenShell sandbox (FR-10, PRD D13), without the Docker socket.

A sandbox's main process (the agent) is a child of ``openshell-sandbox`` that started when
OpenShell says it did (``main_process_started_at_ms``): the recorded v0.1.4 fixtures match to
within 0.1 s. Any process under it belongs to that sandbox. A sandbox may also name its agent type
with the label ``openvigie.agent=<type>``, which counts even when no process matches.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from openvigie.adapters.base import ProcessInfo
from openvigie.model import SandboxState
from openvigie.openshell.cli import Inventory, SandboxView

AGENT_LABEL = "openvigie.agent"
MATCH_WINDOW_S = 1.0
MAX_DEPTH = 64
# The launcher inside each sandbox; /proc truncates names to 15 characters
LAUNCHER_NAMES = ("openshell-sandbox", "openshell-sandb")
AGENT_TYPE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")  # same shape as Adapter.agent_type

Probe = Callable[[int], tuple[SandboxState, str | None]]


def main_processes(
    inventory: Inventory, processes: Sequence[ProcessInfo]
) -> dict[int, SandboxView]:
    """pid → sandbox, for each sandbox whose main process can be told apart unambiguously."""
    launchers = {p.pid for p in processes if p.name in LAUNCHER_NAMES}
    found: dict[int, SandboxView] = {}
    for view in inventory.sandboxes:
        started_ms = view.sandbox.main_process_started_at_ms
        if not started_ms:
            continue
        started = started_ms / 1000
        candidates = [
            p.pid
            for p in processes
            if p.ppid in launchers and abs(p.create_time - started) <= MATCH_WINDOW_S
        ]
        if len(candidates) == 1:
            found[candidates[0]] = view
    return found


def labelled_agent_type(view: SandboxView) -> str | None:
    """The agent type a sandbox declares with ``openvigie.agent=<type>``, if well formed."""
    value = view.sandbox.labels.get(AGENT_LABEL)
    return value if value and AGENT_TYPE.match(value) else None


def kind(view: SandboxView) -> str:
    """``AgentInstance.sandbox_kind`` for an agent in this sandbox."""
    return f"openshell:{view.sandbox.name}"


class OpenShellProbe:
    """``HostContext.sandbox_probe`` that names the OpenShell sandbox a process runs in.

    Falls back to the generic probe (launcher, namespaces, cgroups) for everything else.
    """

    def __init__(self, fallback: Probe, processes: Callable[[], Sequence[ProcessInfo]]) -> None:
        self.fallback, self.processes = fallback, processes
        self.mains: dict[int, SandboxView] = {}

    def update(self, inventory: Inventory) -> None:
        """After each OpenShell read. The last mapping is kept while OpenShell is unreachable."""
        if inventory.state == "ok":
            self.mains = main_processes(inventory, self.processes())
        elif inventory.state in ("not_installed", "unsupported"):
            self.mains = {}

    def __call__(self, pid: int) -> tuple[SandboxState, str | None]:
        if self.mains:
            by_pid = {p.pid: p for p in self.processes()}
            current = pid
            for _ in range(MAX_DEPTH):
                if current in self.mains:
                    return SandboxState.YES, kind(self.mains[current])
                proc = by_pid.get(current)
                if proc is None or proc.ppid in (0, current):
                    break
                current = proc.ppid
        return self.fallback(pid)
