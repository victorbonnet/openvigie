"""The collector loop: detect agents, track status, push changes to the hub (FR-1, FR-2).

One thread, one loop. Every fast tick (default 5 s) refreshes the process snapshot, runs each
adapter's ``detect``, updates status and pushes what changed, so status reaches the UI within
the 10 s freshness budget. A full snapshot is re-sent every heartbeat. When the hub is
unreachable the loop keeps running: status events wait in a bounded queue and pushes back off.
"""

from __future__ import annotations

import logging
import random
import resource
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from openvigie.adapters.base import HostContext
from openvigie.adapters.registry import Discovery, run_detect
from openvigie.collector.client import HubClient, HubError
from openvigie.collector.status import StatusTracker
from openvigie.config import CollectorSettings
from openvigie.model import SCHEMA_VERSION, AdapterHealth, AgentInstance, StatusEvent, to_dict
from openvigie.redact import redact_obj

log = logging.getLogger(__name__)

HEARTBEAT_S = 60.0
MAX_PENDING_EVENTS = 10_000
MAX_BACKOFF_S = 60.0
UNAUTHORIZED_RETRY_S = 300.0


def rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


@dataclass
class PushState:
    sent_agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_events: deque[StatusEvent] = field(
        default_factory=lambda: deque(maxlen=MAX_PENDING_EVENTS)
    )
    last_full_push: float = float("-inf")
    failures: int = 0
    retry_at: float = 0.0
    unauthorized: bool = False


class Collector:
    def __init__(
        self,
        settings: CollectorSettings,
        ctx: HostContext,
        discovery: Discovery,
        client: HubClient,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.ctx = ctx
        self.discovery = discovery
        self.client = client
        self.monotonic = monotonic
        self.tracker = StatusTracker(
            timedelta(seconds=settings.active_window_s),
            grace=timedelta(seconds=settings.fast_tick_s),
        )
        self.state = PushState()
        self.ticks = 0

    # --- one tick ---------------------------------------------------------------------------

    def observe(self) -> tuple[list[AgentInstance], list[AdapterHealth]]:
        self.ctx.refresh()
        agents: list[AgentInstance] = []
        health = list(self.discovery.failures)
        for adapter in self.discovery.adapters:
            found, adapter_health = run_detect(adapter, self.ctx)
            agents.extend(found)
            health.append(adapter_health)
        agents, events = self.tracker.observe(agents, self.ctx.now())
        self.state.pending_events.extend(events)
        return agents, health

    def build_payload(
        self, agents: list[AgentInstance], health: list[AdapterHealth], full: bool
    ) -> dict[str, Any]:
        agent_dicts = {a.agent_id: to_dict(a) for a in agents}
        health_dicts = {h.adapter: to_dict(h) for h in health}
        if not full:
            agent_dicts = {
                k: v for k, v in agent_dicts.items() if self.state.sent_agents.get(k) != v
            }
            health_dicts = {
                k: v for k, v in health_dicts.items() if self.state.sent_health.get(k) != v
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "host": to_dict(self.ctx.host),
            "sent_at": self.ctx.now().isoformat(),
            "agents": list(agent_dicts.values()),
            "status_events": [to_dict(e) for e in self.state.pending_events],
            "adapter_health": list(health_dicts.values()),
        }

    def tick(self) -> bool:
        """Observe and push. Returns True if the hub accepted the push (or nothing was due)."""
        self.ticks += 1
        agents, health = self.observe()
        now = self.monotonic()
        if now < self.state.retry_at:
            return False
        full = now - self.state.last_full_push >= HEARTBEAT_S
        payload = self.build_payload(agents, health, full)
        try:
            self.client.post("/api/v1/ingest", redact_obj(payload))
        except HubError as exc:
            self._failed(exc, now)
            return False
        self._succeeded(payload, full, now)
        if self.ticks % 60 == 1:
            log.debug("tick %d: %d agents, peak RSS %.1f MiB", self.ticks, len(agents), rss_mib())
        return True

    def _succeeded(self, payload: dict[str, Any], full: bool, now: float) -> None:
        for item in payload["agents"]:
            self.state.sent_agents[item["agent_id"]] = item
        for item in payload["adapter_health"]:
            self.state.sent_health[item["adapter"]] = item
        for _ in payload["status_events"]:
            self.state.pending_events.popleft()
        if full:
            self.state.last_full_push = now
        if self.state.failures or self.state.unauthorized:
            log.warning("hub reachable again; pushes resumed")
        self.state.failures, self.state.retry_at, self.state.unauthorized = 0, 0.0, False

    def _failed(self, exc: HubError, now: float) -> None:
        self.state.failures += 1
        self.state.last_full_push = float("-inf")  # resend everything once the hub is back
        if exc.status == 401:
            if not self.state.unauthorized:
                log.error("the hub rejected this host's token (revoked?); run `openvigie enrol`")
            self.state.unauthorized = True
            self.state.retry_at = now + UNAUTHORIZED_RETRY_S
            return
        backoff = min(MAX_BACKOFF_S, 2.0 ** min(self.state.failures, 6))
        self.state.retry_at = now + backoff * random.uniform(0.5, 1.0)  # noqa: S311 - jitter
        level = logging.WARNING if self.state.failures in (1, 10) else logging.DEBUG
        log.log(level, "push failed (%s); retrying in about %.0fs", exc, backoff)

    # --- loop --------------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        interval = self.settings.fast_tick_s
        while not stop.is_set():
            started = self.monotonic()
            try:
                self.tick()
            except Exception:  # never die on a bad tick
                log.exception("collector tick failed")
            stop.wait(max(0.0, interval - (self.monotonic() - started)))
