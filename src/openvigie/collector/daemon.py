"""The collector loop: detect agents, track status, push changes to the hub (FR-1, FR-2, FR-3).

One thread, one loop. Every fast tick (default 5 s) refreshes the process snapshot, runs each
adapter's ``detect``, updates status and pushes what changed, so status reaches the UI within
the 10 s freshness budget. A full snapshot is re-sent every heartbeat. When the hub is
unreachable the loop keeps running: status events wait in a bounded queue and pushes back off.

Every slow tick (default 30 s) runs each adapter's ``collect`` and pushes usage. If the hub
can't take it, the payload goes to the durable spool. Cursors are committed only once the data
is acknowledged or spooled (at-least-once; the hub deduplicates).

Spans (FR-6) are built and sent only while the hub says it exports traces (``traces.wanted``
in its ingest reply). Content goes into them only if both the hub and this collector's
``export_content`` allow it.
"""

from __future__ import annotations

import json
import logging
import random
import resource
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from openvigie.adapters.base import Adapter, AdapterState, HostContext, TraceOptions
from openvigie.adapters.registry import Discovery, run_collect, run_detect
from openvigie.collector.client import HubClient, HubError
from openvigie.collector.otlp import Arbiter, OtlpReceiver, OtlpUsage
from openvigie.collector.state import StateStore
from openvigie.collector.status import StatusTracker
from openvigie.config import CollectorSettings
from openvigie.model import SCHEMA_VERSION, AdapterHealth, AgentInstance, StatusEvent, to_dict
from openvigie.redact import redact_obj

log = logging.getLogger(__name__)

HEARTBEAT_S = 60.0
MAX_PENDING_EVENTS = 10_000
USAGE_CHUNK = 2000
SPAN_CHUNK = 1000
SPAN_CHUNK_BYTES = 1024 * 1024  # the hub refuses request bodies over 2 MiB
SPOOL_FLUSH_PER_TICK = 20
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
        store: StateStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        otlp: OtlpReceiver | None = None,
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
        self.store = store
        self.agents: list[AgentInstance] = []
        self.last_slow = float("-inf")
        self.adapter_states: dict[str, AdapterState] = {}
        self.otlp = otlp
        self.arbiter = Arbiter(settings.otlp.usage_source)

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
        self.agents = agents
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
        """Observe and push; then, when due, collect and push usage.

        Inventory goes first so the hub knows every agent before it receives its usage.
        Returns True if the hub accepted the inventory push.
        """
        self.ticks += 1
        agents, health = self.observe()
        now = self.monotonic()
        pushed = self._push_inventory(agents, health, now)
        if pushed and now - self.last_slow >= self.settings.slow_tick_s:
            self.last_slow = now
            self.slow_tick(now)
        if self.ticks % 60 == 1:
            log.debug("tick %d: %d agents, peak RSS %.1f MiB", self.ticks, len(agents), rss_mib())
        return pushed

    def _push_inventory(
        self, agents: list[AgentInstance], health: list[AdapterHealth], now: float
    ) -> bool:
        if now < self.state.retry_at:
            return False
        full = now - self.state.last_full_push >= HEARTBEAT_S
        payload = self.build_payload(agents, health, full)
        try:
            reply = self.client.post("/api/v1/ingest", redact_obj(payload))
        except HubError as exc:
            self._failed(exc, now)
            return False
        self._succeeded(payload, full, now)
        self._apply_traces(reply)
        return True

    def _apply_traces(self, reply: dict[str, Any]) -> None:
        """Follow the hub's wish for spans; content needs this collector's consent too."""
        traces = reply.get("traces")
        traces = traces if isinstance(traces, dict) else {}
        wanted = traces.get("wanted") is True
        content = wanted and traces.get("export_content") is True and self.settings.export_content
        options = TraceOptions(enabled=wanted, export_content=content)
        if options != self.ctx.traces:
            log.info("trace export %s%s", "on" if wanted else "off",
                     " (with content)" if content else "")  # fmt: skip
            self.ctx.traces = options

    # --- usage (slow tick) --------------------------------------------------------------------

    def adapter_state(self, adapter: Adapter) -> AdapterState:
        if adapter.agent_type not in self.adapter_states:
            since = datetime.now(UTC) - timedelta(days=self.settings.backfill_days)
            self.adapter_states[adapter.agent_type] = (
                self.store.adapter_state(adapter.agent_type, since)
                if self.store
                else AdapterState(backfill_since=since)
            )
        return self.adapter_states[adapter.agent_type]

    def usage_payload(
        self, usage: list[Any], sessions: list[Any], spans: list[Any] | None = None
    ) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "host": to_dict(self.ctx.host),
            "sent_at": self.ctx.now().isoformat(),
            "usage": usage,
            "sessions": sessions,
        }
        if spans:
            payload["spans"] = spans
        if self.store:
            payload["collector"] = {"spool_dropped": self.store.dropped()}
        return payload

    def slow_tick(self, now: float) -> None:
        by_type = {a.agent_type: a for a in self.discovery.adapters}
        usage: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        spans: list[dict[str, Any]] = []
        touched: list[AdapterState] = []
        reported = self._otlp_by_agent()
        for agent in self.agents:
            adapter = by_type.get(agent.agent_type)
            if adapter is None:
                continue
            state = self.adapter_state(adapter)
            from_otlp = self.arbiter.otlp(agent, reported.get(agent.agent_id, []), state)
            usage += [to_dict(r) for r in from_otlp]
            batch, _ = run_collect(adapter, self.ctx, agent, state)
            if batch is None:
                if from_otlp:
                    touched.append(state)
                continue
            usage += [to_dict(r) for r in self.arbiter.logs(agent, batch.usage, state)]
            sessions += [to_dict(s) for s in batch.sessions]
            spans += [to_dict(s) for s in batch.spans]
            touched.append(state)
        if not touched:
            return
        payloads = [
            self.usage_payload(usage[i : i + USAGE_CHUNK], sessions if i == 0 else [])
            for i in range(0, max(len(usage), 1), USAGE_CHUNK)
        ]
        if not usage and not sessions:
            payloads = []
        payloads += [self.usage_payload([], [], chunk) for chunk in _span_chunks(spans)]
        delivered = self._flush_spool(now) and all(self._send_usage(p, now) for p in payloads)
        if not delivered:
            if self.store is None:
                for state in touched:
                    state.rollback()
                return
            for p in payloads:
                self.store.spool_put(redact_obj(p))
        for state in touched:
            state.commit()  # data is now with the hub or in the durable spool

    def _otlp_by_agent(self) -> dict[str, list[OtlpUsage]]:
        """OTLP usage received since the last slow tick, given to the first agent of its type."""
        if self.otlp is None:
            return {}
        grouped: dict[str, list[OtlpUsage]] = {}
        owners: dict[str, str] = {}
        for agent in sorted(self.agents, key=lambda a: a.config_root or ""):
            owners.setdefault(agent.agent_type, agent.agent_id)
        orphans = 0
        for item in self.otlp.drain():
            owner = owners.get(item.agent_type)
            if owner is None:
                orphans += 1
                continue
            grouped.setdefault(owner, []).append(item)
        if orphans:
            log.warning("OTLP: %d usage events from agents not detected on this host dropped",
                        orphans)  # fmt: skip
        return grouped

    def _send_usage(self, payload: dict[str, Any], now: float) -> bool:
        if now < self.state.retry_at:
            return False
        try:
            reply = self.client.post("/api/v1/ingest", redact_obj(payload))
        except HubError as exc:
            self._failed(exc, now)
            return False
        if "usage" in reply.get("ignored", []):
            log.warning("the hub ignored usage data; upgrade the hub. Keeping it in the spool.")
            return False
        return True

    def _flush_spool(self, now: float) -> bool:
        """Send spooled payloads oldest first; True when the spool is empty."""
        if self.store is None:
            return True
        for row_id, payload in self.store.spool_peek(SPOOL_FLUSH_PER_TICK):
            if not self._send_usage(payload, now):
                return False
            self.store.spool_ack(row_id)
        return self.store.spool_stats()[0] == 0

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


def _span_chunks(spans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split spans so each push stays well under the hub's body limit."""
    chunks: list[list[dict[str, Any]]] = []
    size = 0
    for span in spans:
        cost = len(json.dumps(span))
        if not chunks or len(chunks[-1]) >= SPAN_CHUNK or size + cost > SPAN_CHUNK_BYTES:
            chunks.append([])
            size = 0
        chunks[-1].append(span)
        size += cost
    return chunks
