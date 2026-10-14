"""One-shot local inventory: what openvigie sees on this machine (``openvigie scan``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from openvigie.adapters.base import HostContext
from openvigie.adapters.registry import Discovery, run_detect
from openvigie.collector.status import StatusTracker
from openvigie.model import (
    SCHEMA_VERSION,
    AdapterHealth,
    AgentInstance,
    HealthState,
    to_dict,
)
from openvigie.redact import redact_obj


@dataclass
class ScanResult:
    ctx: HostContext
    agents: list[AgentInstance] = field(default_factory=list)
    health: list[AdapterHealth] = field(default_factory=list)


def run_scan(
    ctx: HostContext,
    discovery: Discovery,
    active_window: timedelta,
    only: str | None = None,
) -> ScanResult:
    result = ScanResult(ctx)
    result.health.extend(f for f in discovery.failures if only in (None, f.adapter))
    agents: list[AgentInstance] = []
    for adapter in discovery.adapters:
        if only and adapter.agent_type != only:
            continue
        found, health = run_detect(adapter, ctx)
        agents.extend(found)
        result.health.append(health)
    result.agents, _ = StatusTracker(active_window).observe(agents, ctx.now())
    return result


def to_json(result: ScanResult) -> str:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "host": to_dict(result.ctx.host),
        "agents": [to_dict(a) for a in result.agents],
        "adapters": [to_dict(h) for h in result.health],
    }
    return json.dumps(redact_obj(payload), indent=2)


def ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "unknown"
    seconds = int((now - when).total_seconds())
    if seconds < 0:
        return "in the future"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


def to_text(result: ScanResult) -> str:
    now = result.ctx.now()
    host = result.ctx.host
    lines = [f"openvigie scan: {host.hostname} ({host.os}/{host.arch})", ""]
    if result.agents:
        rows = [["TYPE", "NAME", "VERSION", "STATUS", "SANDBOXED", "LAST ACTIVITY", "CONFIG"]]
        for a in sorted(result.agents, key=lambda a: (a.agent_type, a.config_root or "")):
            sandbox = a.sandboxed.value + (f" ({a.sandbox_kind})" if a.sandbox_kind else "")
            rows.append(
                [
                    a.agent_type,
                    a.name,
                    a.version or "unknown",
                    a.status.value,
                    sandbox,
                    ago(a.last_activity_at, now),
                    a.config_root or "",
                ]
            )
        lines.append(_table(rows))
    else:
        lines.append(
            "No agents found. openvigie looks for Claude Code (~/.claude) and Codex (~/.codex)\n"
            "in the homes listed in collector.toml (default: your home). Run with -v for details."
        )
    if result.health:
        lines += ["", "Adapters:"]
        for h in sorted(result.health, key=lambda h: h.adapter):
            reason = f": {h.reason}" if h.reason else ""
            mark = "ok" if h.state is HealthState.OK else h.state.value
            lines.append(f"  {h.adapter:<14} {mark}{reason}")
    return redact_obj("\n".join(lines))  # type: ignore[no-any-return]
