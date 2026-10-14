from pathlib import Path

from openvigie.adapters.base import ProcessInfo
from openvigie.adapters.testing import fake_process
from openvigie.collector.sandbox import SandboxProbe
from openvigie.model import SandboxState

SELF = 1000
AGENT = 2000


def fake_proc(root: Path, pid: int, ns: dict[str, str], cgroup: str = "0::/user.slice\n") -> None:
    nsdir = root / str(pid) / "ns"
    nsdir.mkdir(parents=True)
    for name, target in ns.items():
        (nsdir / name).symlink_to(f"{name}:[{target}]")
    (root / str(pid) / "cgroup").write_text(cgroup)


HOST_NS = {"mnt": "1", "net": "2", "user": "3", "pid": "4"}


def probe(tmp_path: Path, procs: list[ProcessInfo] | None = None, **kw: object) -> SandboxProbe:
    processes = procs or [fake_process(AGENT, "claude", ppid=1)]
    return SandboxProbe(
        lambda: processes,
        system=str(kw.get("system", "linux")),
        proc_root=tmp_path,
        self_pid=SELF,
    )


def test_same_namespaces_means_not_sandboxed(tmp_path: Path) -> None:
    fake_proc(tmp_path, SELF, HOST_NS)
    fake_proc(tmp_path, AGENT, HOST_NS)
    assert probe(tmp_path)(AGENT) == (SandboxState.NO, None)


def test_different_mount_and_net_namespaces(tmp_path: Path) -> None:
    fake_proc(tmp_path, SELF, HOST_NS)
    fake_proc(tmp_path, AGENT, {**HOST_NS, "mnt": "9", "net": "8"})
    assert probe(tmp_path)(AGENT) == (SandboxState.YES, "namespaces")


def test_container_cgroup(tmp_path: Path) -> None:
    fake_proc(tmp_path, SELF, HOST_NS)
    fake_proc(tmp_path, AGENT, HOST_NS, cgroup="0::/system.slice/docker-abc.scope\n")
    assert probe(tmp_path)(AGENT) == (SandboxState.YES, "container")


def test_bwrap_ancestor(tmp_path: Path) -> None:
    procs = [
        fake_process(AGENT, "claude", ppid=1500),
        fake_process(1500, "bwrap", ppid=1400),
        fake_process(1400, "bash", ppid=1),
    ]
    assert probe(tmp_path, procs)(AGENT) == (SandboxState.YES, "bwrap")


def test_openshell_ancestor_wins_on_macos(tmp_path: Path) -> None:
    procs = [fake_process(AGENT, "codex", ppid=1500), fake_process(1500, "sandbox-exec", ppid=1)]
    assert probe(tmp_path, procs, system="darwin")(AGENT) == (SandboxState.YES, "seatbelt")


def test_macos_without_launcher_is_unknown_not_no(tmp_path: Path) -> None:
    assert probe(tmp_path, system="darwin")(AGENT) == (SandboxState.UNKNOWN, None)


def test_permission_denied_is_unknown(tmp_path: Path) -> None:
    def denied(path: Path) -> str:
        raise PermissionError(13, "denied")

    p = SandboxProbe(lambda: [], system="linux", proc_root=tmp_path, self_pid=SELF, readlink=denied)
    assert p(AGENT) == (SandboxState.UNKNOWN, None)


def test_missing_process_is_unknown(tmp_path: Path) -> None:
    fake_proc(tmp_path, SELF, HOST_NS)
    assert probe(tmp_path)(4242) == (SandboxState.UNKNOWN, None)


def test_ancestry_cycle_terminates(tmp_path: Path) -> None:
    procs = [fake_process(AGENT, "a", ppid=3000), fake_process(3000, "b", ppid=AGENT)]
    fake_proc(tmp_path, SELF, HOST_NS)
    fake_proc(tmp_path, AGENT, HOST_NS)
    assert probe(tmp_path, procs)(AGENT) == (SandboxState.NO, None)
