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

Commands (D14) arrive in the ingest reply too. ``CommandRunner`` runs only the actions this
host allows, and the results go out with the next inventory push.
"""

from __future__ import annotations

import dataclasses
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
from openvigie.adapters.registry import Discovery, run_capabilities, run_collect, run_detect
from openvigie.collector.client import HubClient, HubError
from openvigie.collector.commands import CommandRunner
from openvigie.collector.otlp import Arbiter, OtlpReceiver, OtlpUsage
from openvigie.collector.state import StateStore
from openvigie.collector.status import StatusTracker
from openvigie.config import CollectorSettings
from openvigie.model import SCHEMA_VERSION, AdapterHealth, AgentInstance, StatusEvent, to_dict
from openvigie.openshell.cli import Inventory, Reader, inventory_health
from openvigie.openshell.mapping import OpenShellProbe, records
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
CAPABILITIES_REFRESH_S = 3600.0
MAX_PENDING_RESULTS = 100


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


@dataclass
class _Collected:
    """What one slow tick gathered, as wire dicts, and the adapter states to commit after."""

    usage: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    touched: list[AdapterState] = field(default_factory=list)


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
        openshell: Reader | None = None,
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
        self.capabilities_sent: dict[str, str] = {}
        self.capabilities_sent_at = float("-inf")
        self.arbiter = Arbiter(settings.otlp.usage_source)
        self.openshell = openshell
        self.openshell_inventory = Inventory("not_installed")
        self.sandboxes_sent: str | None = None  # digest of the last list sent; None: never
        self.sandboxes_sent_at = float("-inf")
        self.commands = CommandRunner(settings.allow_actions, store)
        self.command_results: list[dict[str, Any]] = []  # waiting for the next push

    # --- one tick ---------------------------------------------------------------------------

    def observe(self) -> tuple[list[AgentInstance], list[AdapterHealth]]:
        self.ctx.refresh()
        agents: list[AgentInstance] = []
        health = list(self.discovery.failures)
        for adapter in self.discovery.adapters:
            found, adapter_health = run_detect(adapter, self.ctx)
            agents.extend(found)
            health.append(adapter_health)
        if (openshell := inventory_health(self.openshell_inventory)) is not None:
            health.append(openshell)
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
        payload = {
            "schema_version": SCHEMA_VERSION,
            "host": to_dict(self.ctx.host),
            "sent_at": self.ctx.now().isoformat(),
            "agents": list(agent_dicts.values()),
            "status_events": [to_dict(e) for e in self.state.pending_events],
            "adapter_health": list(health_dicts.values()),
        }
        if self.command_results:
            payload["command_results"] = list(self.command_results)
        return payload

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
        self._run_commands(reply)
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

    def _run_commands(self, reply: dict[str, Any]) -> None:
        results = self.commands.run_all(reply.get("commands"))
        self.command_results.extend(to_dict(r) for r in results)
        del self.command_results[:-MAX_PENDING_RESULTS]

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
        self,
        usage: list[Any],
        sessions: list[Any],
        spans: list[Any] | None = None,
        capabilities: list[Any] | None = None,
        sandboxes: list[Any] | None = None,
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
        if capabilities:
            payload["capabilities"] = capabilities
        if sandboxes is not None:  # [] is meaningful: the host has no sandboxes any more
            payload["sandboxes"] = sandboxes
        if self.store:
            payload["collector"] = {"spool_dropped": self.store.dropped()}
        return payload

    def slow_tick(self, now: float) -> None:
        """Collect usage, sessions, spans and capability changes, and send them to the hub."""
        self._read_openshell()
        by_type = {a.agent_type: a for a in self.discovery.adapters}
        found = self._collect_all(by_type)
        capabilities = self._changed_capabilities(by_type, now)
        sandboxes = self._changed_sandboxes(now)
        if not found.touched and not capabilities and sandboxes is None:
            return
        payloads = self._usage_payloads(found, capabilities)
        if sandboxes is not None:
            payloads.append(self.usage_payload([], [], sandboxes=sandboxes))
        delivered = self._flush_spool(now) and all(self._send_usage(p, now) for p in payloads)
        if not delivered:
            if self.store is None:
                for state in found.touched:
                    state.rollback()
                return
            for p in payloads:
                self.store.spool_put(redact_obj(p))
        for state in found.touched:
            state.commit()  # data is now with the hub or in the durable spool

    def _read_openshell(self) -> None:
        """Refresh the OpenShell inventory, and with it which agent runs in which sandbox."""
        if self.openshell is None:
            return
        self.openshell_inventory = self.openshell.read()
        if isinstance(self.ctx.sandbox_probe, OpenShellProbe):
            self.ctx.sandbox_probe.update(self.openshell_inventory)

    def _changed_sandboxes(self, now: float) -> list[dict[str, Any]] | None:
        """This host's full sandbox list when it changed (or hourly); ``None``: nothing to send."""
        inventory = self.openshell_inventory
        if inventory.state == "ok":
            boxes = records(inventory, self.ctx.host.host_id, self.agents, self.ctx.now())
            body = [to_dict(b) for b in boxes]
        elif inventory.state in ("not_installed", "unsupported"):
            if self.sandboxes_sent in (None, "[]"):
                return None  # nothing shown on the hub
            body = []  # OpenShell went away: clear what the hub shows, once
        else:
            return None  # unreachable or never seen: keep what the hub has
        key = json.dumps([{k: v for k, v in b.items() if k != "observed_at"} for b in body],
                         sort_keys=True)  # fmt: skip
        refresh = now - self.sandboxes_sent_at >= CAPABILITIES_REFRESH_S
        if key == self.sandboxes_sent and not refresh:
            return None
        self.sandboxes_sent, self.sandboxes_sent_at = key, now
        return body

    def _collect_all(self, by_type: dict[str, Adapter]) -> _Collected:
        """Run every adapter's ``collect``, with OTLP usage arbitrated against log usage."""
        found = _Collected()
        reported = self._otlp_by_agent()
        for agent in self.agents:
            adapter = by_type.get(agent.agent_type)
            if adapter is None:
                continue
            state = self.adapter_state(adapter)
            from_otlp = self.arbiter.otlp(agent, reported.get(agent.agent_id, []), state)
            found.usage += [to_dict(r) for r in from_otlp]
            batch, _ = run_collect(adapter, self.ctx, agent, state)
            if batch is None:
                if from_otlp:
                    found.touched.append(state)
                continue
            found.usage += [to_dict(r) for r in self.arbiter.logs(agent, batch.usage, state)]
            found.sessions += [to_dict(s) for s in batch.sessions]
            found.spans += [to_dict(s) for s in batch.spans]
            found.touched.append(state)
        found.spans += self._otlp_spans()
        return found

    def _usage_payloads(
        self, found: _Collected, capabilities: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Pushes of bounded size: usage chunks (sessions ride the first), spans, capabilities."""
        usage, sessions = found.usage, found.sessions
        payloads = []
        if usage or sessions:
            payloads = [
                self.usage_payload(usage[i : i + USAGE_CHUNK], sessions if i == 0 else [])
                for i in range(0, max(len(usage), 1), USAGE_CHUNK)
            ]
        payloads += [self.usage_payload([], [], chunk) for chunk in _span_chunks(found.spans)]
        if capabilities:
            payloads.append(self.usage_payload([], [], capabilities=capabilities))
        return payloads

    def _changed_capabilities(
        self, by_type: dict[str, Adapter], now: float
    ) -> list[dict[str, Any]]:
        """Capability snapshots that changed, plus a refresh of all of them every so often."""
        refresh = now - self.capabilities_sent_at >= CAPABILITIES_REFRESH_S
        out = []
        for agent in self.agents:
            adapter = by_type.get(agent.agent_type)
            caps = run_capabilities(adapter, self.ctx, agent) if adapter else None
            if caps is None:
                continue
            body = to_dict(caps)
            key = json.dumps({k: v for k, v in body.items() if k != "observed_at"}, sort_keys=True)
            if refresh or self.capabilities_sent.get(agent.agent_id) != key:
                self.capabilities_sent[agent.agent_id] = key
                out.append(body)
        if refresh:
            self.capabilities_sent_at = now
        return out

    def _otlp_spans(self) -> list[dict[str, Any]]:
        """Agent-emitted spans, credited like OTLP usage; dropped unless the hub wants traces."""
        if self.otlp is None:
            return []
        received = self.otlp.drain_spans()
        if not self.ctx.traces.enabled:
            return []
        owners = self._owners()
        return [to_dict(dataclasses.replace(span, agent_id=owners[agent_type]))
                for agent_type, span in received if agent_type in owners]  # fmt: skip

    def _owners(self) -> dict[str, str]:
        """Agent type → the agent credited with its OTLP data (first by config folder)."""
        owners: dict[str, str] = {}
        for agent in sorted(self.agents, key=lambda a: a.config_root or ""):
            owners.setdefault(agent.agent_type, agent.agent_id)
        return owners

    def _otlp_by_agent(self) -> dict[str, list[OtlpUsage]]:
        """OTLP usage received since the last slow tick, given to the first agent of its type."""
        if self.otlp is None:
            return {}
        grouped: dict[str, list[OtlpUsage]] = {}
        owners = self._owners()
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
        del self.command_results[: len(payload.get("command_results", ()))]
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
