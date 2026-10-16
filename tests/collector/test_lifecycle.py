"""Stopping and killing an agent's processes (FR-13, D16), with real child processes."""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import psutil
import pytest

from openvigie.adapters.base import ProcessInfo
from openvigie.collector.commands import CommandRunner, agent_actions
from openvigie.collector.lifecycle import LifecycleError, signal_agent
from openvigie.model import CommandStatus

IGNORES_TERM = ("import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)")  # fmt: skip


def info(proc: subprocess.Popen[Any], *, start_offset: float = 0.0) -> ProcessInfo:
    p = psutil.Process(proc.pid)
    return ProcessInfo(proc.pid, os.getpid(), p.name(), "", None,
                       p.create_time() + start_offset, ())  # fmt: skip


@pytest.fixture
def spawn() -> Iterator[Any]:
    started: list[subprocess.Popen[Any]] = []

    def run(*argv: str) -> subprocess.Popen[Any]:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)  # noqa: S603
        started.append(proc)
        return proc

    yield run
    for proc in started:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def gone(proc: subprocess.Popen[Any]) -> bool:
    return proc.poll() is not None


def test_stop_terminates_the_process_and_its_children(spawn: Any) -> None:
    parent = spawn("sh", "-c", "sleep 60 & sleep 60 & wait")
    children: list[psutil.Process] = []
    for _ in range(100):  # until the shell has started both
        children = psutil.Process(parent.pid).children()
        if len(children) == 2:
            break
        time.sleep(0.02)
    assert len(children) == 2
    assert signal_agent([info(parent)], "stop") == "stopped 3 processes"
    assert gone(parent) and not any(c.is_running() for c in children)


def test_stop_escalates_to_sigkill(spawn: Any) -> None:
    stubborn = spawn(sys.executable, "-c", IGNORES_TERM)
    assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == "ready"
    result = signal_agent([info(stubborn)], "stop", grace=0.5)
    assert result == "stopped 1 process (1 needed SIGKILL)" and gone(stubborn)


def test_kill_is_immediate(spawn: Any) -> None:
    stubborn = spawn(sys.executable, "-c", IGNORES_TERM)
    assert stubborn.stdout is not None and stubborn.stdout.readline()
    assert signal_agent([info(stubborn)], "kill") == "killed 1 process" and gone(stubborn)


def test_a_reused_pid_is_never_signalled(spawn: Any) -> None:
    other = spawn("sleep", "60")
    with pytest.raises(LifecycleError, match="no running process"):
        signal_agent([info(other, start_offset=-100.0)], "kill")
    assert not gone(other)


def test_protected_and_vanished_processes_are_skipped(spawn: Any) -> None:
    me = ProcessInfo(os.getpid(), 0, "python", "", None, psutil.Process().create_time(), ())
    init = ProcessInfo(1, 0, "init", "", None, 0.0, ())
    done = spawn("true")
    done.wait()
    with pytest.raises(LifecycleError, match="no running process"):
        signal_agent([me, init, ProcessInfo(done.pid, 0, "true", "", None, 0.0, ())], "kill")
    assert psutil.Process().is_running()


def test_not_permitted_is_reported() -> None:
    class Denied:
        pid = 4242

        def create_time(self) -> float:
            return 1.0

        def children(self, recursive: bool) -> list[Any]:
            return []

        def terminate(self) -> None:
            raise psutil.AccessDenied(4242)

    fake = SimpleNamespace(Process=lambda pid: Denied(), NoSuchProcess=psutil.NoSuchProcess,
                           ZombieProcess=psutil.ZombieProcess, AccessDenied=psutil.AccessDenied,
                           wait_procs=psutil.wait_procs)  # fmt: skip
    with pytest.raises(LifecycleError, match="not permitted: run the collector as the agent"):
        signal_agent(
            [ProcessInfo(4242, 1, "claude", "", "alice", 1.0, ())],
            "stop",
            ps=cast(ModuleType, fake),
        )


def test_collector_actions(spawn: Any) -> None:
    agent = spawn("sleep", "60")
    attributed = {"a1": [info(agent)]}
    actions = agent_actions(lambda agent_id: attributed.get(agent_id, []))
    runner = CommandRunner(("agent.stop",), actions=actions)

    def command(cid: str, action: str, **args: str) -> dict[str, Any]:
        return {"command_id": cid * 32, "action": action, "args": args,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}  # fmt: skip

    result = runner.run(command("a", "agent.stop", agent_id="a1"))
    assert result is not None and result.status is CommandStatus.SUCCEEDED and gone(agent)
    result = runner.run(command("b", "agent.stop", agent_id="unknown"))
    assert result is not None and result.detail == "the agent has no running process"
    result = runner.run(command("c", "agent.stop"))
    assert result is not None and result.detail == "missing argument agent_id"
    result = runner.run(command("d", "agent.kill", agent_id="a1"))
    assert result is not None and result.status is CommandStatus.REFUSED  # not allowed here
