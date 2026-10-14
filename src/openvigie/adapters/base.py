"""Adapter plugin interface (FR-7).

An adapter teaches openvigie about one agent type and answers three questions:

* ``detect``: is this agent on the host?
* ``collect``: what is it doing (activity, sessions, usage, spans)?
* ``capabilities``: what can it access? Reserved for P1 (FR-8).

Adapters touch the host only through ``HostContext``, so the same code runs against a real host
or a fixture tree in tests.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    HealthState,
    Host,
    SandboxState,
    Session,
    Span,
    UsageRecord,
)
from openvigie.safeio import ReadStats

INTERFACE_VERSION = 1
AGENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


@dataclass(frozen=True)
class ProcessInfo:
    """A running process. ``cmdline`` is for matching only: never serialize or log it."""

    pid: int
    ppid: int
    name: str
    exe: str
    username: str | None
    create_time: float
    cmdline: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class ListeningPort:
    port: int
    address: str
    pid: int | None


def _no_sandbox_info(pid: int) -> tuple[SandboxState, str | None]:
    return SandboxState.UNKNOWN, None


@dataclass(frozen=True)
class TraceOptions:
    """Whether ``collect`` builds GenAI spans (FR-6). Off by default: spans cost memory and CPU."""

    enabled: bool = False
    # prompts, outputs and tool arguments/results; always redacted and truncated when on
    export_content: bool = False


@dataclass
class HostContext:
    """Everything an adapter may know about the host. Snapshots are cached per tick."""

    host: Host
    homes: tuple[Path, ...]
    environ: Mapping[str, str] = field(default_factory=dict)
    process_source: Callable[[], Sequence[ProcessInfo]] = lambda: ()
    port_source: Callable[[], Sequence[ListeningPort] | None] = lambda: None
    sandbox_probe: Callable[[int], tuple[SandboxState, str | None]] = _no_sandbox_info
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    traces: TraceOptions = field(default_factory=TraceOptions)
    _processes: Sequence[ProcessInfo] | None = field(default=None, init=False, repr=False)
    _ports: Sequence[ListeningPort] | None = field(default=None, init=False, repr=False)
    _ports_loaded: bool = field(default=False, init=False, repr=False)

    def refresh(self) -> None:
        """Drop cached snapshots; the collector calls this at the start of every tick."""
        self._processes, self._ports, self._ports_loaded = None, None, False

    def processes(self) -> Sequence[ProcessInfo]:
        if self._processes is None:
            self._processes = tuple(self.process_source())
        return self._processes

    def ports(self) -> Sequence[ListeningPort] | None:
        """Listening sockets, or ``None`` when they can't be observed (e.g. macOS, no root)."""
        if not self._ports_loaded:
            ports = self.port_source()
            self._ports = None if ports is None else tuple(ports)
            self._ports_loaded = True
        return self._ports

    def env(self, name: str) -> str | None:
        return self.environ.get(name)

    def now(self) -> datetime:
        return self.clock()

    def sandbox_of(self, pid: int) -> tuple[SandboxState, str | None]:
        return self.sandbox_probe(pid)


class AdapterState:
    """Per-adapter key/value store for cursors, with stage → commit semantics.

    Adapters ``stage`` new cursors while collecting; the collector ``commit``s them only after
    the hub acknowledged the data derived from them (at-least-once delivery).
    """

    def __init__(
        self,
        committed: dict[str, dict[str, Any]] | None = None,
        *,
        backfill_since: datetime | None = None,
    ) -> None:
        self.committed: dict[str, dict[str, Any]] = committed or {}
        self.staged: dict[str, dict[str, Any]] = {}
        # On first read of a file (no cursor), skip records older than this.
        self.backfill_since = backfill_since

    def get(self, key: str) -> dict[str, Any] | None:
        return self.staged.get(key, self.committed.get(key))

    def stage(self, key: str, value: dict[str, Any]) -> None:
        self.staged[key] = value

    def commit(self) -> None:
        self.committed.update(self.staged)
        self.staged.clear()

    def rollback(self) -> None:
        self.staged.clear()


@dataclass
class ActivityBatch:
    last_activity_at: datetime | None = None
    sessions: list[Session] = field(default_factory=list)
    usage: list[UsageRecord] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    stats: ReadStats = field(default_factory=ReadStats)


class Adapter(ABC):
    agent_type: ClassVar[str]
    display_name: ClassVar[str]
    interface_version: ClassVar[int] = INTERFACE_VERSION

    def __init__(self) -> None:
        self.stats = ReadStats()
        self.notes: list[str] = []

    @abstractmethod
    def detect(self, ctx: HostContext) -> list[AgentInstance]:
        """Return one instance per install of this agent on the host (may be empty)."""

    def collect(self, ctx: HostContext, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
        """Return activity since the cursors in ``state``. Default: nothing to report."""
        return ActivityBatch()

    def capabilities(self, ctx: HostContext, agent: AgentInstance) -> Any:
        """What the agent can access (FR-8, P1)."""
        raise NotImplementedError

    def health(self) -> AdapterHealth:
        """Default: degraded when files had to be skipped, with the first note as reason."""
        degraded = self.stats.parse_errors or self.stats.oversized or self.notes
        return AdapterHealth(
            adapter=self.agent_type,
            state=HealthState.DEGRADED if degraded else HealthState.OK,
            reason="; ".join(self.notes)[:500] or None,
            parse_errors=self.stats.parse_errors,
            skipped=self.stats.oversized,
        )
