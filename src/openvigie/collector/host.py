"""Build the real ``HostContext`` for this machine from collector settings."""

from __future__ import annotations

import os
from pathlib import Path

from openvigie.adapters.base import HostContext
from openvigie.collector.probes import listening_ports, load_host, snapshot_processes
from openvigie.collector.sandbox import SandboxProbe
from openvigie.config import CollectorSettings, state_dir


def build_context(settings: CollectorSettings, state: Path | None = None) -> HostContext:
    ctx = HostContext(
        host=load_host(state or state_dir()),
        homes=settings.homes,
        environ=dict(os.environ),
        process_source=snapshot_processes,
        port_source=listening_ports,
    )
    ctx.sandbox_probe = SandboxProbe(ctx.processes)
    return ctx
