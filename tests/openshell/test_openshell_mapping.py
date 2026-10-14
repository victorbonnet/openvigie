"""Mapping agents to sandboxes, on the process tree recorded with OpenShell v0.1.4."""

from pathlib import Path

from openvigie.adapters.base import ProcessInfo
from openvigie.model import SandboxState
from openvigie.openshell.cli import Inventory, SandboxView
from openvigie.openshell.mapping import OpenShellProbe, labelled_agent_type, main_processes
from openvigie.openshell.models import Sandbox

FIXTURES = Path(__file__).parent / "fixtures" / "v0.1.4"
SANDBOX = Sandbox.model_validate_json((FIXTURES / "sandbox-get.json").read_text())
INVENTORY = Inventory("ok", sandboxes=(SandboxView(SANDBOX, None),))


def main_start() -> float:
    """The recorded main process's start time, the way psutil computes it."""
    stat = (FIXTURES / "main-stat.txt").read_text()
    ticks = int(stat[stat.rindex(")") + 2 :].split()[19])
    btime = int((FIXTURES / "proc-btime.txt").read_text().split()[1])
    return btime + ticks / int((FIXTURES / "clk-tck.txt").read_text())


def processes() -> list[ProcessInfo]:
    """The recorded `ps` tree; only the main process's start time was recorded."""
    out = []
    for line in (FIXTURES / "ps.txt").read_text().splitlines()[1:]:
        pid, ppid, user, *rest = line.split()
        words = [w for w in rest if w not in ("\\_", "|")]
        # ps truncates the COMMAND column; psutil reports the executable's full name
        name = words[1].rsplit("/", 1)[-1] if len(words) > 1 else words[0]
        out.append(ProcessInfo(int(pid), int(ppid), name, "", user, 0.0, ()))
    return [p if p.pid != 2955 else ProcessInfo(p.pid, p.ppid, p.name, "", p.username,
                                                 main_start(), ()) for p in out]  # fmt: skip


def test_main_process_is_found_by_start_time() -> None:
    assert main_processes(INVENTORY, processes()) == {2955: INVENTORY.sandboxes[0]}


def test_a_process_with_the_right_start_but_another_parent_is_not_matched() -> None:
    procs = [p for p in processes() if p.pid != 2955]
    procs.append(ProcessInfo(2955, 1, "sleep", "", "packer", main_start(), ()))
    assert main_processes(INVENTORY, procs) == {}


def test_two_candidates_are_ambiguous() -> None:
    twin = ProcessInfo(9999, 2835, "claude", "", "packer", main_start() + 0.5, ())
    assert main_processes(INVENTORY, [*processes(), twin]) == {}


def test_probe_names_the_sandbox_for_the_agent_and_its_children() -> None:
    child = ProcessInfo(4000, 2955, "node", "", "packer", 0.0, ())
    procs = [*processes(), child]
    probe = OpenShellProbe(lambda pid: (SandboxState.NO, None), lambda: procs)
    probe.update(INVENTORY)
    assert probe(2955) == (SandboxState.YES, "openshell:ov-fixture")
    assert probe(4000) == (SandboxState.YES, "openshell:ov-fixture")
    assert probe(2546) == (SandboxState.NO, None)  # the gateway: falls back
    probe.update(Inventory("unreachable"))
    assert probe(2955)[1] == "openshell:ov-fixture"  # keeps the last known mapping
    probe.update(Inventory("not_installed"))
    assert probe(2955) == (SandboxState.NO, None)


def test_label_names_the_agent_type() -> None:
    assert labelled_agent_type(INVENTORY.sandboxes[0]) == "claude-code"
    odd = SANDBOX.model_copy(update={"labels": {"openvigie.agent": "Rm -rf"}})
    assert labelled_agent_type(SandboxView(odd, None)) is None
