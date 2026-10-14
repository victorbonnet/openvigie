"""Discover adapters through the ``openvigie.adapters`` entry-point group and run them safely.

Built-in adapters (modules under ``openvigie.adapters.``) load unless disabled. Third-party
adapters run in-process with the collector's privileges, so they load **only** when listed in
``enable_plugins``; otherwise they are never imported.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points

from openvigie.adapters.base import (
    AGENT_TYPE_RE,
    INTERFACE_VERSION,
    ActivityBatch,
    Adapter,
    AdapterState,
    HostContext,
)
from openvigie.model import AdapterHealth, AgentInstance, HealthState
from openvigie.redact import redact

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "openvigie.adapters"
BUILTIN_PREFIX = "openvigie.adapters."
SLOW_CALL_S = 2.0


@dataclass
class Discovery:
    adapters: list[Adapter] = field(default_factory=list)
    failures: list[AdapterHealth] = field(default_factory=list)
    skipped_plugins: list[str] = field(default_factory=list)


def _entry_points() -> Iterable[EntryPoint]:
    return entry_points(group=ENTRY_POINT_GROUP)


def discover(
    enable_plugins: Iterable[str] = (), disable: Iterable[str] = (), *, quiet: bool = False
) -> Discovery:
    enabled, disabled = set(enable_plugins), set(disable)
    found = Discovery()
    seen: set[str] = set()
    for ep in sorted(_entry_points(), key=lambda e: e.name):
        builtin = ep.value.startswith(BUILTIN_PREFIX)
        if ep.name in disabled or ep.name in seen:
            continue
        if not builtin and ep.name not in enabled:
            found.skipped_plugins.append(ep.name)
            continue
        try:
            cls = ep.load()
            if not (isinstance(cls, type) and issubclass(cls, Adapter)):
                raise TypeError("entry point is not an Adapter subclass")
            if cls.agent_type != ep.name or not AGENT_TYPE_RE.match(cls.agent_type):
                raise ValueError(f"agent_type must equal the entry-point name {ep.name!r}")
            if cls.interface_version != INTERFACE_VERSION:
                raise ValueError(f"interface_version {cls.interface_version} is not supported")
            found.adapters.append(cls())
            seen.add(ep.name)
        except Exception as exc:  # a broken plugin must never stop the collector
            found.failures.append(_error(ep.name, exc))
            if not quiet:
                log.warning("adapter %s failed to load: %s", ep.name, exc)
    return found


def _error(name: str, exc: BaseException) -> AdapterHealth:
    reason = redact(f"{type(exc).__name__}: {exc}")[:500]
    return AdapterHealth(adapter=name, state=HealthState.ERROR, reason=reason)


def _slow(adapter: Adapter, what: str, started: float) -> None:
    elapsed = time.monotonic() - started
    if elapsed > SLOW_CALL_S:
        adapter.notes.append(f"{what} took {elapsed:.1f}s")
        log.warning("adapter %s: %s took %.1fs", adapter.agent_type, what, elapsed)


def run_detect(adapter: Adapter, ctx: HostContext) -> tuple[list[AgentInstance], AdapterHealth]:
    adapter.notes.clear()
    started = time.monotonic()
    try:
        agents = adapter.detect(ctx)
    except Exception as exc:
        log.warning("adapter %s: detect failed: %s", adapter.agent_type, exc)
        return [], _error(adapter.agent_type, exc)
    _slow(adapter, "detect", started)
    return agents, adapter.health()


def run_collect(
    adapter: Adapter, ctx: HostContext, agent: AgentInstance, state: AdapterState
) -> tuple[ActivityBatch | None, AdapterHealth]:
    started = time.monotonic()
    try:
        batch = adapter.collect(ctx, agent, state)
    except Exception as exc:
        state.rollback()
        log.warning("adapter %s: collect failed: %s", adapter.agent_type, exc)
        return None, _error(adapter.agent_type, exc)
    adapter.stats.add(batch.stats)
    _slow(adapter, "collect", started)
    return batch, adapter.health()
