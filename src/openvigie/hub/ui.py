"""Server-rendered web UI (FastAPI + Jinja2 + HTMX). GET-only in the read-only MVP."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from openvigie.adapters.spans import trace_id
from openvigie.hub.api import DateParam, date_range
from openvigie.hub.charts import OTHER, Series, assign_slots, compact, hbars, money, stacked_bars
from openvigie.hub.langfuse import TraceLink, project_url, trace_link
from openvigie.hub.reports import UsageRow, usage_report
from openvigie.hub.store.inventory import AgentRow, HostRow, Inventory
from openvigie.hub.store.spans import SpanQueue
from openvigie.hub.store.usage import UsageStore
from openvigie.hub.templating import render
from openvigie.model import AgentStatus, HealthState, Session
from openvigie.pricing import PriceTable

router = APIRouter(include_in_schema=False)

Filter = Annotated[str | None, Query(max_length=64)]


def ago(when: datetime | None, now: datetime | None = None) -> str:
    if when is None:
        return "unknown"
    seconds = int(((now or datetime.now(UTC)) - when).total_seconds())
    if seconds < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def _inventory(request: Request) -> Inventory:
    inventory: Inventory = request.app.state.inventory
    return inventory


def _filters(type: str | None, host: str | None, status: str | None) -> dict[str, str]:
    return {k: v for k, v in (("type", type), ("host", host), ("status", status)) if v}


@router.get("/", response_class=HTMLResponse)
def agents_page(
    request: Request, type: Filter = None, host: Filter = None, status: Filter = None
) -> HTMLResponse:
    inventory = _inventory(request)
    rows = inventory.agents(host_id=host, agent_type=type, status=status)
    everything = inventory.agents()
    filters = _filters(type, host, status)
    return render(
        request,
        "agents.html",
        title="Agents",
        rows=rows,
        filters=filters,
        query=urlencode(filters),
        types=sorted({r.agent.agent_type for r in everything}),
        hosts=sorted({(r.agent.host_id, r.hostname) for r in everything}, key=lambda h: h[1]),
        statuses=[s.value for s in AgentStatus],
        any_agents=bool(everything),
    )


@router.get("/agents/rows", response_class=HTMLResponse)
def agent_rows(
    request: Request, type: Filter = None, host: Filter = None, status: Filter = None
) -> HTMLResponse:
    filters = _filters(type, host, status)
    rows = _inventory(request).agents(host_id=host, agent_type=type, status=status)
    return render(request, "_agent_rows.html", rows=rows, query=urlencode(filters))


AgentId = Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


def _agent_or_404(request: Request, agent_id: str) -> AgentRow:
    row = _inventory(request).agent(agent_id)
    if row is None:
        raise HTTPException(404, "agent not found")
    return row


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
def agent_page(request: Request, agent_id: AgentId) -> HTMLResponse:
    row = _agent_or_404(request, agent_id)
    inventory = _inventory(request)
    health = [
        h for h in inventory.adapter_health(row.agent.host_id)
        if h.health.adapter == row.agent.agent_type
    ]  # fmt: skip
    return render(
        request,
        "agent.html",
        title=row.agent.name,
        row=row,
        health=health[0] if health else None,
        history=inventory.status_history(agent_id, limit=50),
        usage=agent_usage(request, agent_id),
        money=money,
        compact=compact,
    )


@dataclass(frozen=True)
class SessionView:
    usage: UsageRow
    session: Session | None
    trace: TraceLink | None = None  # None: trace export is off


@dataclass(frozen=True)
class AgentUsage:
    total: UsageRow
    chart: Markup
    models: list[UsageRow]
    sessions: list[SessionView]
    langfuse_project: str | None = None


def agent_usage(request: Request, agent_id: str, days: int = 30) -> AgentUsage:
    state = request.app.state
    begin, finish = date_range(state.tz, None, None, default_days=days)
    report = partial(usage_report, state.db, state.prices, start=begin, end=finish,
                     tz=state.tz, agent_id=agent_id)  # fmt: skip
    per_day = report(group_by=["day"])
    day_list = []
    day = begin.date()
    while day < finish.date():
        day_list.append(day.isoformat())
        day += timedelta(days=1)
    series = [Series(agent_id, "cost", 1, {r.key["day"]: r.cost for r in per_day.rows})]
    known = {s.session_id: s for s in UsageStore(state.db).sessions(agent_id)}
    sessions = sorted(report(group_by=["session"]).rows, key=lambda r: r.cost, reverse=True)
    views = [SessionView(r, known.get(r.key["session"])) for r in sessions[:20]]
    project = None
    if state.exporter:
        lf = state.exporter.settings
        project = project_url(lf)
        traces = {v.usage.key["session"]: trace_id(agent_id, v.usage.key["session"])
                  for v in views if v.usage.key["session"]}  # fmt: skip
        done = SpanQueue(state.db).exported(list(traces.values()))
        views = [
            replace(v, trace=trace_link(lf, traces[sid], traces[sid] in done))
            if (sid := v.usage.key["session"]) in traces else v
            for v in views
        ]  # fmt: skip
    return AgentUsage(
        total=per_day.total,
        chart=stacked_bars(day_list, series),
        models=sorted(report(group_by=["model"]).rows, key=lambda r: r.cost, reverse=True),
        sessions=views,
        langfuse_project=project,
    )


@router.get("/prices", response_class=HTMLResponse)
def prices_page(request: Request) -> HTMLResponse:
    state = request.app.state
    table: PriceTable = state.prices
    begin, finish = date_range(state.tz, None, None, default_days=90)
    seen = usage_report(state.db, table, start=begin, end=finish, group_by=["model"], tz=state.tz)
    unpriced = [r for r in seen.rows if r.unpriced_tokens]
    prices = sorted(table.models.values(), key=lambda p: (p.provider, p.model_id))
    return render(
        request,
        "prices.html",
        title="Prices",
        prices=prices,
        sources=table.sources,
        included=table.cached_input_included,
        unpriced=unpriced,
        override_path=str(table.override_path or "prices.toml"),
        compact=compact,
    )


@router.get("/agents/{agent_id}/status", response_class=HTMLResponse)
def agent_status(request: Request, agent_id: AgentId) -> HTMLResponse:
    row = _agent_or_404(request, agent_id)
    return render(request, "_agent_status.html", row=row)


STALE_AFTER = timedelta(seconds=20)  # about 3 collector ticks without a report
SKEW_WARNING_S = 60.0
HostId = Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]


@dataclass(frozen=True)
class HostView:
    row: HostRow
    stale: bool
    skewed: bool
    counts: dict[str, int]
    unhealthy: int


def host_views(inventory: Inventory, now: datetime) -> list[HostView]:
    agents = inventory.agents()
    health = inventory.adapter_health()
    views = []
    for row in inventory.hosts():
        mine = [a.agent for a in agents if a.agent.host_id == row.host.host_id]
        counts = {s.value: sum(a.status is s for a in mine) for s in AgentStatus}
        views.append(
            HostView(
                row=row,
                stale=now - row.last_seen > STALE_AFTER,
                skewed=abs(row.clock_skew_s or 0.0) > SKEW_WARNING_S,
                counts={k: v for k, v in counts.items() if v},
                unhealthy=sum(
                    h.host_id == row.host.host_id and h.health.state is not HealthState.OK
                    for h in health
                ),
            )
        )
    return views


@router.get("/hosts", response_class=HTMLResponse)
def hosts_page(request: Request) -> HTMLResponse:
    views = host_views(_inventory(request), datetime.now(UTC))
    return render(request, "hosts.html", title="Hosts", views=views)


@router.get("/hosts/{host_id}", response_class=HTMLResponse)
def host_page(request: Request, host_id: HostId) -> HTMLResponse:
    inventory = _inventory(request)
    view = next((v for v in host_views(inventory, datetime.now(UTC))
                 if v.row.host.host_id == host_id), None)  # fmt: skip
    if view is None:
        raise HTTPException(404, "host not found")
    return render(
        request,
        "host.html",
        title=view.row.host.hostname,
        view=view,
        rows=inventory.agents(host_id=host_id),
        query=urlencode({"host": host_id}),
        health=inventory.adapter_health(host_id),
    )


RANGES = (7, 30, 90)


@router.get("/usage", response_class=HTMLResponse)
def usage_page(
    request: Request,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
    start: Annotated[DateParam, Query(alias="from")] = None,
    end: Annotated[DateParam, Query(alias="to")] = None,
) -> HTMLResponse:
    state = request.app.state
    tz = state.tz
    begin, finish = date_range(tz, start, end, default_days=days)
    report = partial(usage_report, state.db, state.prices, start=begin, end=finish, tz=tz)
    per_agent = report(group_by=["agent"])
    per_day = report(group_by=["agent", "day"])
    per_model = report(group_by=["model"])
    per_session = report(group_by=["agent", "session"])

    day_list = []
    day = begin.date()
    while day < finish.date():
        day_list.append(day.isoformat())
        day += timedelta(days=1)
    slots = assign_slots([r.key["agent"] for r in per_agent.rows])
    values: dict[str, dict[str, Decimal]] = {}
    for row in per_day.rows:
        bucket = row.key["agent"] if row.key["agent"] in slots else OTHER
        values.setdefault(bucket, {})
        values[bucket][row.key["day"]] = values[bucket].get(row.key["day"], Decimal(0)) + row.cost
    labels = {r.key["agent"]: r.labels["agent"] for r in per_agent.rows}
    series = [
        Series(key, labels.get(key, OTHER), slots.get(key, 0), values[key])
        for key in sorted(values, key=lambda k: (k == OTHER, slots.get(k, 99)))
    ]
    top_agent = max(per_agent.rows, key=lambda r: r.cost, default=None)
    sessions = sorted(per_session.rows, key=lambda r: r.cost, reverse=True)[:10]
    models = sorted(per_model.rows, key=lambda r: r.tokens, reverse=True)
    return render(
        request,
        "usage.html",
        title="Usage",
        begin=begin.date(),
        last=(finish - timedelta(days=1)).date(),
        days=days,
        ranges=RANGES,
        custom=bool(start or end),
        total=per_agent.total,
        top_agent=top_agent,
        series=series,
        daily_chart=stacked_bars(day_list, series),
        model_chart=hbars([(r.labels["model"], float(r.tokens)) for r in models]),
        per_day=per_day.rows,
        models=models,
        sessions=sessions,
        money=money,
        compact=compact,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    state = request.app.state
    exporter = state.exporter
    queue = exporter.queue.stats() if exporter else None
    return render(request, "settings.html", title="Settings", settings=state.settings,
                  tz=state.tz, exporter=exporter, queue=queue)  # fmt: skip
