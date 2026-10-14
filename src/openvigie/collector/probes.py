"""Host probes: identity, process snapshot and listening ports (FR-1).

Process command lines stay in memory for adapter matching only; nothing here serializes them.
Listening ports degrade to ``None`` (unknown) when the OS won't tell us, e.g. macOS without root.
"""

from __future__ import annotations

import logging
import platform
import socket
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import psutil

from openvigie import __version__
from openvigie.adapters.base import ListeningPort, ProcessInfo
from openvigie.config import ConfigError, read_private_file, write_private_file
from openvigie.model import Host

log = logging.getLogger(__name__)

MAX_ARGS = 256
MAX_ARG_LEN = 4096
_ATTRS = ["pid", "ppid", "name", "exe", "username", "create_time", "cmdline"]


def load_host(state_dir: Path) -> Host:
    """This host's identity. The id is a random UUID created on first run, never hardware ids."""
    id_file = state_dir / "host_id"
    try:
        host_id = read_private_file(id_file)
    except FileNotFoundError:
        host_id = uuid.uuid4().hex
        write_private_file(id_file, host_id + "\n")
    if not host_id or len(host_id) > 64:
        raise ConfigError(f"{id_file}: invalid host id")
    return Host(
        host_id=host_id,
        hostname=socket.gethostname()[:256],
        os=platform.system().lower(),
        arch=platform.machine().lower(),
        collector_version=__version__,
    )


def _str(value: Any, limit: int = 1024) -> str:
    return value[:limit] if isinstance(value, str) else ""


def snapshot_processes(ps: ModuleType = psutil) -> list[ProcessInfo]:
    out: list[ProcessInfo] = []
    for proc in ps.process_iter(_ATTRS, ad_value=None):
        try:
            info = proc.info
        except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess):
            continue
        cmdline = info.get("cmdline") or ()
        out.append(
            ProcessInfo(
                pid=int(info["pid"]),
                ppid=int(info.get("ppid") or 0),
                name=_str(info.get("name"), 256),
                exe=_str(info.get("exe")),
                username=info.get("username") if isinstance(info.get("username"), str) else None,
                create_time=float(info.get("create_time") or 0.0),
                cmdline=tuple(_str(a, MAX_ARG_LEN) for a in list(cmdline)[:MAX_ARGS]),
            )
        )
    return out


def _listen(conns: Any, ps: ModuleType, pid: int | None = None) -> list[ListeningPort]:
    ports = []
    for conn in conns:
        if conn.status != ps.CONN_LISTEN or not conn.laddr:
            continue
        ports.append(
            ListeningPort(
                port=int(conn.laddr.port),
                address=str(conn.laddr.ip),
                pid=pid if pid is not None else conn.pid,
            )
        )
    return ports


def listening_ports(ps: ModuleType = psutil) -> list[ListeningPort] | None:
    """System-wide listening TCP sockets; per-process fallback; ``None`` if unobservable."""
    try:
        return _listen(ps.net_connections(kind="tcp"), ps)
    except (ps.AccessDenied, PermissionError):
        pass
    ports: list[ListeningPort] = []
    any_visible = False
    for proc in ps.process_iter(["pid"], ad_value=None):
        try:
            conns = proc.net_connections(kind="tcp")
        except (ps.NoSuchProcess, ps.AccessDenied, ps.ZombieProcess, PermissionError):
            continue
        any_visible = True
        ports.extend(_listen(conns, ps, proc.pid))
    if not any_visible:
        log.debug("listening ports are not observable on this host")
        return None
    return ports
