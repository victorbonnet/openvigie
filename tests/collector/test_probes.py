import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openvigie.adapters.base import ProcessInfo
from openvigie.collector import probes
from openvigie.model import to_dict

Addr = namedtuple("Addr", "ip port")
Conn = namedtuple("Conn", "fd family type laddr raddr status pid")


class AccessDeniedError(Exception):
    pass


class NoSuchProcessError(Exception):
    pass


def fake_ps(
    procs: list[dict[str, Any]],
    *,
    system_conns: list[Conn] | None = None,
    per_proc: dict[int, list[Conn]] | None = None,
) -> Any:
    def process_iter(attrs: list[str], ad_value: Any = None) -> Any:
        for info in procs:
            proc = SimpleNamespace(pid=info["pid"], info=info)

            def net_connections(kind: str, pid: int = info["pid"]) -> list[Conn]:
                if per_proc is None or pid not in per_proc:
                    raise AccessDeniedError()
                return per_proc[pid]

            proc.net_connections = net_connections
            yield proc

    def net_connections(kind: str) -> list[Conn]:
        if system_conns is None:
            raise AccessDeniedError()
        return system_conns

    return SimpleNamespace(
        process_iter=process_iter,
        net_connections=net_connections,
        AccessDenied=AccessDeniedError,
        NoSuchProcess=NoSuchProcessError,
        ZombieProcess=NoSuchProcessError,
        CONN_LISTEN="LISTEN",
    )


PROC: dict[str, Any] = {
    "pid": 42,
    "ppid": 1,
    "name": "claude",
    "exe": "/usr/local/bin/claude",
    "username": "pi",
    "create_time": 1.5,
    "cmdline": ["claude", "--api-key", "sk-ant-api03-" + "S" * 40],
}


def test_snapshot_processes_maps_fields_and_tolerates_none() -> None:
    procs = [PROC, {"pid": 7, "ppid": None, "name": None, "exe": None, "username": None,
                    "create_time": None, "cmdline": None}]  # fmt: skip
    out = probes.snapshot_processes(fake_ps(procs))
    assert out[0] == ProcessInfo(42, 1, "claude", "/usr/local/bin/claude", "pi", 1.5,
                                 tuple(PROC["cmdline"]))  # fmt: skip
    assert out[1].name == "" and out[1].cmdline == () and out[1].username is None


def test_cmdline_is_bounded() -> None:
    proc = dict(PROC, cmdline=["x" * 10_000] * 1000)
    (info,) = probes.snapshot_processes(fake_ps([proc]))
    assert len(info.cmdline) == probes.MAX_ARGS
    assert len(info.cmdline[0]) == probes.MAX_ARG_LEN


def test_cmdline_never_serialized() -> None:
    (info,) = probes.snapshot_processes(fake_ps([PROC]))
    assert "sk-ant" not in repr(info)
    with pytest.raises(TypeError):
        to_dict(info)


def test_listening_ports_system_wide() -> None:
    conns = [
        Conn(3, 2, 1, Addr("127.0.0.1", 18789), (), "LISTEN", 42),
        Conn(4, 2, 1, Addr("10.0.0.2", 5555), Addr("1.2.3.4", 443), "ESTABLISHED", 42),
    ]
    ports = probes.listening_ports(fake_ps([PROC], system_conns=conns))
    assert ports is not None
    assert [(p.port, p.pid) for p in ports] == [(18789, 42)]


def test_listening_ports_per_process_fallback() -> None:
    conns = {42: [Conn(3, 2, 1, Addr("127.0.0.1", 7878), (), "LISTEN", None)]}
    ports = probes.listening_ports(fake_ps([PROC], per_proc=conns))
    assert ports is not None
    assert [(p.port, p.pid) for p in ports] == [(7878, 42)]


def test_listening_ports_unknown_when_unobservable() -> None:
    assert probes.listening_ports(fake_ps([PROC])) is None


def test_host_identity_is_random_and_persistent(tmp_path: Path) -> None:
    first = probes.load_host(tmp_path)
    second = probes.load_host(tmp_path)
    assert first.host_id == second.host_id
    assert len(first.host_id) == 32
    assert (tmp_path / "host_id").stat().st_mode & 0o777 == 0o600
    other = probes.load_host(tmp_path / "elsewhere")
    assert other.host_id != first.host_id


def test_real_host_smoke() -> None:
    start = time.perf_counter()
    procs = probes.snapshot_processes()
    elapsed = time.perf_counter() - start
    assert procs and all(isinstance(p, ProcessInfo) for p in procs)
    ports = probes.listening_ports()
    assert ports is None or isinstance(ports, list)
    print(f"snapshot of {len(procs)} processes took {elapsed * 1000:.1f} ms")
    assert elapsed < 2.0
