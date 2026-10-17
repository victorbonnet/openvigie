"""Server-rendered web UI (FastAPI + Jinja2 + HTMX). GET only: write actions are in ``admin``."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path as FilePath
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup

from openvigie.adapters.spans import trace_id
from openvigie.hub.api import DateParam, date_range
from openvigie.hub.budgets import evaluate, load_budgets, used
from openvigie.hub.charts import OTHER, Series, assign_slots, compact, hbars, money, stacked_bars
from openvigie.hub.langfuse import TraceLink, project_url, trace_link
from openvigie.hub.reports import Reading, UsageRow, usage_report
from openvigie.hub.riskview import AgentRisk, agent_risks
from openvigie.hub.store.alerts import AlertStore
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.capabilities import CapabilityStore
from openvigie.hub.store.inventory import AgentRow, HostRow, Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.hub.store.spans import SpanQueue
from openvigie.hub.store.usage import UsageStore
from openvigie.hub.templating import render
from openvigie.model import AgentStatus, HealthState, OpenShellSandbox, SandboxState, Session
from openvigie.openshell.starter import StarterPolicy, generate
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


def _filters(
    type: str | None, host: str | None, status: str | None, sort: str | None = None
) -> dict[str, str]:
    pairs = (("type", type), ("host", host), ("status", status), ("sort", sort))
    return {k: v for k, v in pairs if v}


SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3, "none": 4}


def sandbox_badges(request: Request) -> dict[str, OpenShellSandbox]:
    """Agent id → the OpenShell sandbox it runs in, for the sandbox badges (FR-10)."""
    return {a: box for box in SandboxStore(request.app.state.db).all() for a in box.agent_ids}


def _with_risk(
    request: Request, rows: list[AgentRow], sort: str | None
) -> tuple[list[AgentRow], dict[str, AgentRisk]]:
    risks = agent_risks(request.app.state.db, request.app.state.risk_rules, rows)
    if sort:
        rows = _sort_rows(rows, sort, risks, _agent_spend(request))
    return rows, risks


# column → how its natural order and the reverse read, for the header titles
_SORT_ORDERS = {
    "name": ("A→Z", "Z→A"),
    "type": ("A→Z", "Z→A"),
    "host": ("A→Z", "Z→A"),
    "version": ("oldest first", "newest first"),
    "status": ("active first", "stopped first"),
    "risk": ("most risky first", "least risky first"),
    "sandboxed": ("sandboxed first", "unsandboxed first"),
    "usage": ("highest cost first", "lowest cost first"),
    "activity": ("most recent first", "least recent first"),
}
_DESCENDING = {"usage", "activity"}
_STATUS_ORDER = {AgentStatus.ACTIVE: 0, AgentStatus.IDLE: 1, AgentStatus.STOPPED: 2}
_SANDBOX_ORDER = {SandboxState.YES: 0, SandboxState.NO: 1}


def _version_key(version: str) -> tuple[tuple[int, str], ...]:
    """Numbers compare as numbers, so 0.21.5 sorts before 0.162.0."""
    return tuple(
        (int(p), "") if p.isdigit() else (-1, p) for p in re.findall(r"\d+|[^\d.]+", version)
    )


def _usage_key(u: UsageRow | None) -> tuple[Decimal, int] | None:
    """Usage none of which could be priced is unknown, not $0; tokens break ties between lower
    bounds."""
    if u is None or (u.cost == 0 and not u.cost_complete):
        return None
    return u.cost, u.tokens


def _sort_rows(
    rows: list[AgentRow], sort: str, risks: dict[str, AgentRisk], spend: _Spend
) -> list[AgentRow]:
    """Sorts the agent list by one column (FR-5): ``sort=usage`` in the column's natural order,
    ``sort=-usage`` reversed. Rows whose value is unknown stay last either way."""
    column = sort.removeprefix("-")
    keys: dict[str, Callable[[AgentRow], Any]] = {
        "name": lambda r: r.agent.name.casefold(),
        "type": lambda r: r.agent.agent_type,
        "host": lambda r: r.hostname.casefold(),
        "version": lambda r: _version_key(r.agent.version) if r.agent.version else None,
        "status": lambda r: _STATUS_ORDER.get(r.agent.status),
        "risk": lambda r: SEVERITY_ORDER[risks[r.agent.agent_id].level],
        "sandboxed": lambda r: _SANDBOX_ORDER.get(r.agent.sandboxed),
        "usage": lambda r: _usage_key(spend.rows.get(r.agent.agent_id)),
        "activity": lambda r: r.agent.last_activity_at,
    }
    key = keys.get(column)
    if key is None:
        return rows
    known = [r for r in rows if key(r) is not None]
    unknown = [r for r in rows if key(r) is None]
    descending = (column in _DESCENDING) != sort.startswith("-")
    return sorted(known, key=key, reverse=descending) + unknown


@dataclass(frozen=True)
class _SortHeader:
    query: str  # a click sorts by this column, a second click reverses it
    arrow: str  # ↓ natural order, ↑ reversed
    aria_sort: str  # "" when the list isn't sorted by this column
    title: str


# Natural orders that put the largest value first, for aria-sort.
_LARGEST_FIRST = _DESCENDING | {"risk"}


def _sort_headers(filters: dict[str, str]) -> dict[str, _SortHeader]:
    current = filters.get("sort")
    headers = {}
    for column, (natural, reverse) in _SORT_ORDERS.items():
        reversed_ = current == f"-{column}"
        query = urlencode({**filters, "sort": f"-{column}" if current == column else column})
        if current not in (column, f"-{column}"):
            headers[column] = _SortHeader(query, "", "", f"Sort {natural}")
            continue
        ascending = (column in _LARGEST_FIRST) == reversed_
        order = reverse if reversed_ else natural
        headers[column] = _SortHeader(query, "↑" if reversed_ else "↓",
                                      "ascending" if ascending else "descending",
                                      f"Sorted {order}; click to reverse")  # fmt: skip
    return headers


_SPEND_DAYS = 30
# The rows refresh every 5 s and usage barely moves between refreshes; the hub may share a Pi
# with a collector, so the 30-day report is reused for this long.
_SPEND_TTL = 30.0


@dataclass(frozen=True)
class _Spend:
    rows: dict[str, UsageRow]  # agent id → usage; an agent with no usage has no entry
    reading: frozenset[str]  # agents whose history is still being read
    at: float


def _agent_spend(request: Request) -> _Spend:
    """Each agent's usage and estimated cost over the last ``_SPEND_DAYS`` days, for the agent
    list (FR-3, FR-4, FR-5). Missing usage shows as "—", never $0."""
    state = request.app.state
    cached: _Spend | None = getattr(state, "agent_spend", None)
    now = time.monotonic()
    if cached is not None and now - cached.at < _SPEND_TTL:
        return cached
    begin, finish = date_range(state.tz, None, None, default_days=_SPEND_DAYS)
    report = usage_report(state.db, state.prices, start=begin, end=finish, tz=state.tz,
                          group_by=["agent"])  # fmt: skip
    spend = _Spend({r.key["agent"]: r for r in report.rows},
                   frozenset(r.agent_id for r in report.reading), now)  # fmt: skip
    state.agent_spend = spend
    return spend


def _spend_context(request: Request) -> dict[str, object]:
    """What ``_agent_rows.html`` needs to show each agent's usage column."""
    return {"spend": _agent_spend(request), "spend_days": _SPEND_DAYS}


@router.get("/", response_class=HTMLResponse)
def agents_page(
    request: Request,
    type: Filter = None,
    host: Filter = None,
    status: Filter = None,
    sort: Filter = None,
) -> HTMLResponse:
    inventory = _inventory(request)
    rows, risks = _with_risk(request, inventory.agents(host_id=host, agent_type=type,
                                                       status=status), sort)  # fmt: skip
    everything = inventory.agents()
    filters = _filters(type, host, status, sort)
    return render(
        request,
        "agents.html",
        title="Agents",
        rows=rows,
        risks=risks,
        rule_problems=request.app.state.risk_rules.problems,
        filters=filters,
        query=urlencode(filters),
        sort_headers=_sort_headers(filters),
        types=sorted({r.agent.agent_type for r in everything}),
        hosts=sorted({(r.agent.host_id, r.hostname) for r in everything}, key=lambda h: h[1]),
        statuses=[s.value for s in AgentStatus],
        any_agents=bool(everything),
        sandboxes=sandbox_badges(request),
        **_spend_context(request),
    )


@router.get("/agents/rows", response_class=HTMLResponse)
def agent_rows(
    request: Request,
    type: Filter = None,
    host: Filter = None,
    status: Filter = None,
    sort: Filter = None,
) -> HTMLResponse:
    filters = _filters(type, host, status, sort)
    rows, risks = _with_risk(request, _inventory(request).agents(
        host_id=host, agent_type=type, status=status), sort)  # fmt: skip
    return render(request, "_agent_rows.html", rows=rows, risks=risks, query=urlencode(filters),
                  sandboxes=sandbox_badges(request), **_spend_context(request))  # fmt: skip


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
    capabilities = CapabilityStore(request.app.state.db)
    sandboxes = SandboxStore(request.app.state.db)
    sandbox = sandboxes.of_agent(agent_id, row.agent.host_id)
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
        caps=capabilities.get(agent_id),
        risk=agent_risks(request.app.state.db, request.app.state.risk_rules, [row])[agent_id],
        host=inventory.host(row.agent.host_id),
        cap_changes=capabilities.changes(agent_id, limit=20),
        usage=agent_usage(request, agent_id),
        money=money,
        compact=compact,
        sandbox=sandbox,
        starter=starter_view(request, row, sandbox),
        sandbox_changes=sandboxes.changes(sandbox.sandbox_id, limit=10) if sandbox else [],
        sandbox_blocked=sandboxes.still_blocked(sandbox) if sandbox else [],
        sandboxes={agent_id: sandbox} if sandbox else {},
    )


@dataclass(frozen=True)
class SessionView:
    usage: UsageRow
    session: Session | None
    trace: TraceLink | None = None  # None: trace export is off


@dataclass(frozen=True)
class ReadingView:
    """An agent whose history is still being read, worded for the page (FR-3)."""

    agent_id: str
    label: str
    left: str  # e.g. "1.2 GiB left"
    stalled: str | None  # why it won't finish on its own, e.g. "host revoked"; None: progressing


STALLED_AFTER = timedelta(hours=1)


def reading_views(reading: list[Reading], now: datetime | None = None) -> list[ReadingView]:
    now = now or datetime.now(UTC)
    return [
        ReadingView(r.agent_id, r.label,
                    f"{byte_size(r.bytes_left)} left" if r.bytes_left else "amount left unknown",
                    _stalled(r, now))
        for r in reading
    ]  # fmt: skip


def _stalled(r: Reading, now: datetime) -> str | None:
    if r.host_revoked:
        return "host revoked"
    if now - r.host_last_seen > STALLED_AFTER:
        return f"collector not reporting since {ago(r.host_last_seen, now)}"
    return None


def byte_size(n: int) -> str:
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


@dataclass(frozen=True)
class AgentUsage:
    total: UsageRow
    chart: Markup
    models: list[UsageRow]
    sessions: list[SessionView]
    langfuse_project: str | None = None
    reading: list[ReadingView] = field(default_factory=list)


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
        reading=reading_views(per_day.reading),
    )


def override_path(table: PriceTable) -> str:
    """The price override file, home-relative so the page doesn't show the account name."""
    path = table.override_path
    if path is None:
        return "prices.toml"
    home = FilePath.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


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
        override_path=override_path(table),
        compact=compact,
    )


@dataclass(frozen=True)
class StarterView:
    """An agent's starter OpenShell policy, and how it compares with its sandbox (FR-11)."""

    policy: StarterPolicy
    missing: tuple[str, ...]  # hosts the starter allows that the agent's sandbox doesn't
    extra: tuple[str, ...]  # hosts the sandbox allows that the starter doesn't need


def starter_view(request: Request, row: AgentRow, sandbox: OpenShellSandbox | None) -> StarterView:
    caps = CapabilityStore(request.app.state.db).get(row.agent.agent_id)
    policy = generate(row.agent, caps)
    if sandbox is None or sandbox.network_rules is None:
        return StarterView(policy, (), ())  # nothing to compare with
    wanted = {host for rule in policy.rules for host in rule.hosts}
    current = {e.host for rule in sandbox.network_rules for e in rule.endpoints if e.host}
    return StarterView(policy, tuple(sorted(wanted - current)), tuple(sorted(current - wanted)))


@router.get("/agents/{agent_id}/starter-policy.yaml")
def starter_policy(request: Request, agent_id: AgentId) -> Response:
    """The agent's starter policy as a file to review and use (FR-11). Nothing is applied."""
    row = _agent_or_404(request, agent_id)
    policy = generate(row.agent, CapabilityStore(request.app.state.db).get(agent_id))
    name = re.sub(r"[^a-z0-9-]+", "-", row.agent.agent_type.lower()).strip("-") or "agent"
    disposition = f'attachment; filename="openvigie-starter-{name}.yaml"'
    return Response(
        policy.yaml, media_type="application/yaml", headers={"Content-Disposition": disposition}
    )


@router.get("/agents/{agent_id}/status", response_class=HTMLResponse)
def agent_status(request: Request, agent_id: AgentId) -> HTMLResponse:
    row = _agent_or_404(request, agent_id)
    sandbox = SandboxStore(request.app.state.db).of_agent(agent_id, row.agent.host_id)
    return render(request, "_agent_status.html", row=row,
                  sandboxes={agent_id: sandbox} if sandbox else {})  # fmt: skip


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
    high_risk: int = 0


def host_views(
    inventory: Inventory, now: datetime, risks: dict[str, AgentRisk] | None = None
) -> list[HostView]:
    agents = inventory.agents()
    health = inventory.adapter_health()
    risks = risks or {}
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
                high_risk=sum(
                    a.agent_id in risks and risks[a.agent_id].level == "high" for a in mine
                ),
            )
        )
    return views


@router.get("/hosts", response_class=HTMLResponse)
def hosts_page(request: Request) -> HTMLResponse:
    inventory = _inventory(request)
    risks = agent_risks(request.app.state.db, request.app.state.risk_rules, inventory.agents())
    views = host_views(inventory, datetime.now(UTC), risks)
    return render(request, "hosts.html", title="Hosts", views=views)


@router.get("/hosts/{host_id}", response_class=HTMLResponse)
def host_page(request: Request, host_id: HostId) -> HTMLResponse:
    inventory = _inventory(request)
    rows, risks = _with_risk(request, inventory.agents(host_id=host_id), None)
    view = next((v for v in host_views(inventory, datetime.now(UTC), risks)
                 if v.row.host.host_id == host_id), None)  # fmt: skip
    if view is None:
        raise HTTPException(404, "host not found")
    store = SandboxStore(request.app.state.db)
    host_boxes = store.all(host_id)
    return render(
        request,
        "host.html",
        title=view.row.host.hostname,
        view=view,
        rows=rows,
        risks=risks,
        query=urlencode({"host": host_id}),
        health=inventory.adapter_health(host_id),
        sandboxes=sandbox_badges(request),
        host_sandboxes=host_boxes,
        sandbox_changes={b.sandbox_id: store.changes(b.sandbox_id, limit=5) for b in host_boxes},
        sandbox_blocked={b.sandbox_id: store.still_blocked(b) for b in host_boxes},
        agent_names={r.agent.agent_id: r.agent.name for r in rows},
        **_spend_context(request),
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
    table: PriceTable = state.prices
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
        unpriced_models=sorted(r.labels["model"] for r in models if r.unpriced_tokens),
        override_path=override_path(table),
        sessions=sessions,
        reading=reading_views(per_agent.reading),
        money=money,
        compact=compact,
    )


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, before: Annotated[int | None, Query(ge=1)] = None) -> HTMLResponse:
    """Every admin action, newest first (FR-17)."""
    entries = AuditLog(request.app.state.db).entries(limit=100, before_seq=before)
    more = len(entries) == 100 and entries[-1].seq > 1
    return render(request, "audit.html", title="Audit log", entries=entries,
                  next_before=entries[-1].seq if more else None)  # fmt: skip


@router.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request) -> HTMLResponse:
    """Each budget's use in its current period, and recent alerts (FR-14)."""
    budgets = load_budgets()
    statuses = evaluate(request.app.state.db, request.app.state.prices, budgets.budgets,
                        request.app.state.tz, datetime.now(UTC))  # fmt: skip
    return render(request, "budgets.html", title="Budgets", statuses=statuses, budgets=budgets,
                  alerts=AlertStore(request.app.state.db).recent(), used=used)  # fmt: skip


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    state = request.app.state
    exporter = state.exporter
    queue = exporter.queue.stats() if exporter else None
    return render(request, "settings.html", title="Settings", settings=state.settings,
                  tz=state.tz, exporter=exporter, queue=queue)  # fmt: skip
