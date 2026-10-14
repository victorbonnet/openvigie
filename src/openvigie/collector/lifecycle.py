"""Stop or kill an agent's processes on this host (FR-13, PRD D16).

Offers ``signal_agent(processes, mode)``, which returns a line for the hub or raises
``LifecycleError``. The processes come from the adapter that attributed them to the agent: the
hub only ever names an agent, never a PID.

Each process is looked up again just before it is signalled and must have the start time seen
at detection, so a PID reused by another program is never hit; its children go with it. The
collector signals only what the OS lets it signal (no privilege escalation), and never PID 1,
itself or its parent. ``stop`` sends SIGTERM and, after ``GRACE_S``, SIGKILL to whatever is left;
``kill`` sends SIGKILL at once.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from types import ModuleType
from typing import Any, Literal

import psutil

from openvigie.adapters.base import ProcessInfo

GRACE_S = 10.0
KILL_WAIT_S = 3.0
START_TOLERANCE_S = 0.05  # create_time is a float; allow for rounding between reads


class LifecycleError(Exception):
    """Nothing was signalled, or not everything; the message says why."""


def signal_agent(processes: Sequence[ProcessInfo], mode: Literal["stop", "kill"],
                 ps: ModuleType = psutil, grace: float = GRACE_S) -> str:  # fmt: skip
    targets = _targets(processes, ps)
    if not targets:
        raise LifecycleError("the agent has no running process")
    _send(targets, "kill" if mode == "kill" else "terminate", ps)
    _, alive = ps.wait_procs(targets, timeout=KILL_WAIT_S if mode == "kill" else grace)
    forced = 0
    if alive and mode == "stop":
        _send(alive, "kill", ps)
        forced = len(alive)
        _, alive = ps.wait_procs(alive, timeout=KILL_WAIT_S)
    if alive:
        raise LifecycleError(f"{len(alive)} of {len(targets)} processes are still running")
    verb = "killed" if mode == "kill" else "stopped"
    note = f" ({forced} needed SIGKILL)" if mode == "stop" and forced else ""
    return f"{verb} {len(targets)} process{'es' if len(targets) != 1 else ''}{note}"


def _send(procs: Sequence[Any], method: str, ps: ModuleType) -> None:
    for proc in procs:
        try:
            getattr(proc, method)()  # psutil refuses if the PID was reused since the lookup
        except ps.NoSuchProcess:
            continue  # exited on its own
        except ps.AccessDenied:
            raise LifecycleError("not permitted: run the collector as the agent's user") from None


def _targets(processes: Sequence[ProcessInfo], ps: ModuleType) -> list[Any]:
    """The processes still running as detected, and their children, without duplicates."""
    protected = {0, 1, os.getpid(), os.getppid()}
    found: dict[int, Any] = {}
    for info in processes:
        if info.pid in protected:
            continue
        try:
            proc = ps.Process(info.pid)
            if abs(proc.create_time() - info.create_time) > START_TOLERANCE_S:
                continue  # the PID now belongs to another process
            family = [proc, *proc.children(recursive=True)]
        except (ps.NoSuchProcess, ps.ZombieProcess):
            continue
        except ps.AccessDenied:
            raise LifecycleError("not permitted: run the collector as the agent's user") from None
        found.update({p.pid: p for p in family if p.pid not in protected})
    return list(found.values())
