"""Collector-facing JSON API: enrolment and (later) ingest. Bodies are untrusted input."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as day_time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from openvigie.hub.reports import DIMENSIONS, usage_report
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.spans import SpanQueue
from openvigie.hub.store.usage import UsageStore
from openvigie.model import (
    SCHEMA_VERSION,
    AdapterHealth,
    AgentInstance,
    CodecError,
    Host,
    Session,
    Span,
    StatusEvent,
    UsageRecord,
    from_dict,
)

router = APIRouter(prefix="/api/v1")


@dataclass(frozen=True)
class _SentAt:
    at: datetime


FAILURE_WINDOW_S = 60.0
MAX_FAILURES = 10


def now_utc() -> datetime:
    return datetime.now(UTC)


class FailureLimiter:
    """Global limit on failed enrolments; collectors all arrive via loopback, so not per IP."""

    def __init__(self, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW_S) -> None:
        self.limit, self.window = limit, window
        self.failures: deque[float] = deque()
        self.lock = threading.Lock()

    def blocked(self) -> bool:
        with self.lock:
            cutoff = time.monotonic() - self.window
            while self.failures and self.failures[0] < cutoff:
                self.failures.popleft()
            return len(self.failures) >= self.limit

    def record(self) -> None:
        with self.lock:
            self.failures.append(time.monotonic())


def credentials(request: Request) -> Credentials:
    return Credentials(request.app.state.db)


async def json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be a JSON object")
    return body


JsonBody = Annotated[dict[str, Any], Depends(json_body)]


def require_host(request: Request) -> str:
    """Resolve ``Authorization: Bearer <token>`` to a host id, or 401."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    host_id = None
    if scheme.lower() == "bearer" and token:
        host_id = credentials(request).verify_token(token.strip(), now_utc())
    if host_id is None:
        raise HTTPException(401, "invalid or revoked token", {"WWW-Authenticate": "Bearer"})
    return host_id


@router.post("/enrol")
async def enrol(request: Request, body: JsonBody) -> JSONResponse:
    limiter: FailureLimiter = request.app.state.enrol_limiter
    if limiter.blocked():
        raise HTTPException(429, "too many failed enrolments, try again in a minute")
    code = body.get("code")
    try:
        host = from_dict(Host, body.get("host"))
    except CodecError as exc:
        raise HTTPException(422, str(exc)) from None
    now = now_utc()
    creds = credentials(request)
    if not isinstance(code, str) or len(code) > 128 or not creds.code_usable(code, now):
        limiter.record()
        raise HTTPException(403, "invalid, expired or already used enrolment code")
    bound = creds.code_bound_to(code, host.host_id)
    if (creds.has_active_token(host.host_id) or creds.is_revoked(host.host_id)) and not bound:
        raise HTTPException(
            409, "this host is already enrolled or revoked; use `openvigie hub hosts rotate`"
        )
    if not creds.consume_enrol_code(code, host.host_id, now):
        limiter.record()
        raise HTTPException(403, "invalid, expired or already used enrolment code")
    request.app.state.inventory.upsert_host(host, now)
    if bound:
        creds.reinstate(host.host_id)
    token = creds.issue_token(host.host_id, now)
    return JSONResponse({"host_id": host.host_id, "token": token}, status_code=201)


INGEST_LIMITS = {
    "agents": 500,
    "status_events": 5000,
    "adapter_health": 64,
    "usage": 5000,
    "sessions": 1000,
    "spans": 2000,
}
KNOWN_SECTIONS = {"schema_version", "host", "sent_at", "collector", *INGEST_LIMITS}
NEXT_PUSH_S = 5


def _section[T](body: dict[str, Any], name: str, cls: type[T]) -> list[T]:
    items = body.get(name, [])
    if not isinstance(items, list):
        raise HTTPException(422, f"{name} must be a list")
    if len(items) > INGEST_LIMITS[name]:
        raise HTTPException(413, f"{name}: at most {INGEST_LIMITS[name]} items per push")
    try:
        return [from_dict(cls, item) for item in items]
    except CodecError as exc:
        raise HTTPException(422, f"{name}: {exc}") from None


@router.post("/ingest")
async def ingest(
    request: Request, body: JsonBody, host_id: Annotated[str, Depends(require_host)]
) -> dict[str, Any]:
    version = body.get("schema_version")
    if version != SCHEMA_VERSION:
        raise HTTPException(
            422, f"unsupported schema_version (this hub speaks {SCHEMA_VERSION}); upgrade openvigie"
        )
    try:
        host = from_dict(Host, body.get("host"))
        sent_at = from_dict(_SentAt, {"at": body.get("sent_at")}).at
    except CodecError as exc:
        raise HTTPException(422, str(exc)) from None
    if host.host_id != host_id:
        raise HTTPException(403, "token does not belong to this host")
    agents = _section(body, "agents", AgentInstance)
    events = _section(body, "status_events", StatusEvent)
    health = _section(body, "adapter_health", AdapterHealth)
    usage = _section(body, "usage", UsageRecord)
    sessions = _section(body, "sessions", Session)
    spans = _section(body, "spans", Span)
    collector = body.get("collector", {})
    dropped = collector.get("spool_dropped") if isinstance(collector, dict) else None
    if any(a.host_id != host_id for a in agents):
        raise HTTPException(403, "agents must belong to the authenticated host")

    now = now_utc()
    skew = round((sent_at - now).total_seconds(), 1)
    inventory = request.app.state.inventory
    inventory.upsert_host(host, now, clock_skew_s=skew)
    accepted = {
        "agents": inventory.upsert_agents(agents, now),
        "status_events": inventory.add_status_events(events, host_id),
        "adapter_health": len(health),
    }
    inventory.set_adapter_health(host_id, health, now)
    usage_store = UsageStore(request.app.state.db)
    accepted["usage"] = usage_store.add_records(usage, host_id)
    accepted["sessions"] = usage_store.add_sessions(sessions, host_id)
    exporter = request.app.state.exporter
    # spans are only queued when they will be exported; never stored otherwise
    accepted["spans"] = (
        SpanQueue(request.app.state.db).enqueue(spans, host_id, now) if exporter else 0
    )
    if isinstance(dropped, int) and not isinstance(dropped, bool) and 0 <= dropped < 2**31:
        usage_store.set_spool_dropped(host_id, dropped)
    ignored = sorted(set(body) - KNOWN_SECTIONS)
    return {
        "schema_version": SCHEMA_VERSION,
        "accepted": accepted,
        "ignored": ignored,
        "next_push_s": NEXT_PUSH_S,
        # tells collectors whether to build and send spans (FR-6)
        "traces": {
            "wanted": exporter is not None,
            "export_content": bool(exporter and exporter.settings.export_content),
        },
    }


DateParam = Annotated[str | None, Query(max_length=10, pattern=r"^\d{4}-\d{2}-\d{2}$")]


def date_range(
    tz: tzinfo, start: str | None, end: str | None, default_days: int = 30
) -> tuple[datetime, datetime]:
    """[start, end] as local calendar days, returned as an aware [start, end + 1 day) range."""
    today = datetime.now(tz).date()
    last = date.fromisoformat(end) if end else today
    first = date.fromisoformat(start) if start else last - timedelta(days=default_days - 1)
    if first > last or (last - first).days > 366:
        raise HTTPException(422, "date range must be ascending and at most 366 days")
    begin = datetime.combine(first, day_time.min, tzinfo=tz)
    finish = datetime.combine(last + timedelta(days=1), day_time.min, tzinfo=tz)
    return begin, finish


@router.get("/usage")
def usage(
    request: Request,
    group_by: Annotated[str, Query(max_length=64)] = "",
    start: Annotated[DateParam, Query(alias="from")] = None,
    end: Annotated[DateParam, Query(alias="to")] = None,
) -> dict[str, Any]:
    dims = [d for d in group_by.split(",") if d]
    unknown = sorted(set(dims) - set(DIMENSIONS))
    if unknown:
        raise HTTPException(422, f"unknown group_by dimension(s); use {', '.join(DIMENSIONS)}")
    begin, finish = date_range(request.app.state.tz, start, end)
    report = usage_report(
        request.app.state.db, request.app.state.prices,
        start=begin, end=finish, group_by=dims, tz=request.app.state.tz,
    )  # fmt: skip
    return {
        "from": begin.isoformat(),
        "to": finish.isoformat(),
        "group_by": dims,
        "rows": [row.to_json() for row in report.rows],
        "total": report.total.to_json(),
    }
