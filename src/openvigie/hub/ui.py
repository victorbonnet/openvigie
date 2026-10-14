"""Server-rendered web UI (FastAPI + Jinja2 + HTMX). GET-only in the read-only MVP."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from openvigie.hub.store.inventory import Inventory
from openvigie.hub.templating import render
from openvigie.model import AgentStatus

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
