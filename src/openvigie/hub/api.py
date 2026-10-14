"""The hub's JSON API (``/api/v1``): enrolment and ingest for collectors, agents and usage for
scripts. Bodies are untrusted input, validated by the models in ``api_models``."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as day_time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from openvigie.hub.api_models import (
    Accepted,
    AgentOut,
    AgentsResponse,
    AuditEntryOut,
    AuditResponse,
    EnrolRequest,
    EnrolResponse,
    FlagOut,
    IngestRequest,
    IngestResponse,
    RiskOut,
    SandboxesResponse,
    TracesWanted,
    UsageResponse,
    UsageRowOut,
)
from openvigie.hub.reports import DIMENSIONS, usage_report
from openvigie.hub.riskview import agent_risks
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.capabilities import CapabilityStore
from openvigie.hub.store.commands import CommandQueue, StoredCommand
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.hub.store.spans import SpanQueue
from openvigie.hub.store.usage import UsageStore
from openvigie.model import SCHEMA_VERSION, Command

router = APIRouter(prefix="/api/v1")


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


@router.post("/enrol", status_code=201)
async def enrol(request: Request, body: EnrolRequest) -> EnrolResponse:
    limiter: FailureLimiter = request.app.state.enrol_limiter
    if limiter.blocked():
        raise HTTPException(429, "too many failed enrolments, try again in a minute")
    code, host = body.code, body.host
    now = now_utc()
    creds = credentials(request)
    if not creds.code_usable(code, now):
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
    return EnrolResponse(host_id=host.host_id, token=creds.issue_token(host.host_id, now))


NEXT_PUSH_S = 5


@router.post("/ingest")
async def ingest(
    request: Request, body: IngestRequest, host_id: Annotated[str, Depends(require_host)]
) -> IngestResponse:
    if body.schema_version != SCHEMA_VERSION:
        raise HTTPException(
            422, f"unsupported schema_version (this hub speaks {SCHEMA_VERSION}); upgrade openvigie"
        )
    if body.host.host_id != host_id:
        raise HTTPException(403, "token does not belong to this host")
    if any(a.host_id != host_id for a in body.agents):
        raise HTTPException(403, "agents must belong to the authenticated host")
    if any(s.host_id != host_id for s in body.sandboxes or ()):
        raise HTTPException(403, "sandboxes must belong to the authenticated host")

    now = now_utc()
    skew = round((body.sent_at - now).total_seconds(), 1)
    inventory = request.app.state.inventory
    inventory.upsert_host(body.host, now, clock_skew_s=skew)
    agents = inventory.upsert_agents(body.agents, now)
    events = inventory.add_status_events(body.status_events, host_id)
    inventory.set_adapter_health(host_id, body.adapter_health, now)
    usage_store = UsageStore(request.app.state.db)
    exporter = request.app.state.exporter
    accepted = Accepted(
        agents=agents,
        status_events=events,
        adapter_health=len(body.adapter_health),
        usage=usage_store.add_records(body.usage, host_id),
        sessions=usage_store.add_sessions(body.sessions, host_id),
        capabilities=CapabilityStore(request.app.state.db).put(body.capabilities, host_id),
        # spans are only queued when they will be exported; never stored otherwise
        spans=SpanQueue(request.app.state.db).enqueue(body.spans, host_id, now) if exporter else 0,
        sandboxes=(
            0
            if body.sandboxes is None
            else SandboxStore(request.app.state.db).replace(host_id, body.sandboxes, now)
        ),
    )
    if body.collector.spool_dropped is not None:
        usage_store.set_spool_dropped(host_id, body.collector.spool_dropped)
    commands, results = exchange_commands(request, body, now)
    accepted = accepted.model_copy(update={"command_results": results})
    return IngestResponse(
        commands=commands,
        schema_version=SCHEMA_VERSION,
        accepted=accepted,
        ignored=sorted(body.model_extra or {}),
        next_push_s=NEXT_PUSH_S,
        traces=TracesWanted(
            wanted=exporter is not None,
            export_content=bool(exporter and exporter.settings.export_content),
        ),
    )


def exchange_commands(
    request: Request, body: IngestRequest, now: datetime
) -> tuple[list[Command], int]:
    """Store the collector's command results and hand it its due commands; audit every step.
    Returns the commands for the reply and how many results were stored."""
    queue, audit = CommandQueue(request.app.state.db), AuditLog(request.app.state.db)
    host = body.host

    def log(command: StoredCommand, result: str, after: object = None) -> None:
        audit.record(at=now, actor=f"host:{host.hostname}", role="collector",
                     action=f"command.{command.action}", target=host.host_id, result=result,
                     request_id=command.command_id, after=after)  # fmt: skip

    finished = queue.record_results(host.host_id, body.command_results, now)
    for done in finished:
        log(done, done.status, {"detail": done.detail})
    due, expired = queue.deliver(host.host_id, now)
    for command in expired:
        log(command, "expired")
    for command in due:
        log(command, "delivered")
    return [command.wire() for command in due], len(finished)


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


@router.get("/audit")
def audit(
    request: Request,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditResponse:
    """The audit log of admin actions, newest first (FR-17). Readable by every role."""
    entries = AuditLog(request.app.state.db).entries(limit=limit, before_seq=before)
    more = len(entries) == limit and entries[-1].seq > 1
    return AuditResponse(
        entries=[AuditEntryOut(**e.to_json()) for e in entries],
        next_before=entries[-1].seq if more else None,
    )


@router.get("/sandboxes")
def sandboxes(request: Request) -> SandboxesResponse:
    """OpenShell sandboxes with their network rules and the agents in them (FR-10)."""
    return SandboxesResponse(sandboxes=SandboxStore(request.app.state.db).all())


@router.get("/agents")
def agents(request: Request) -> AgentsResponse:
    """Every agent with its status and risk flags (FR-2, FR-9)."""
    rows = request.app.state.inventory.agents()
    risks = agent_risks(request.app.state.db, request.app.state.risk_rules, rows)
    return AgentsResponse(
        agents=[
            AgentOut(
                agent=row.agent,
                hostname=row.hostname,
                risk=RiskOut(
                    level=risks[row.agent.agent_id].level,
                    flags=[FlagOut.of(f) for f in risks[row.agent.agent_id].flags],
                ),
            )
            for row in rows
        ],
        rule_problems=request.app.state.risk_rules.problems,
    )


@router.get("/usage")
def usage(
    request: Request,
    group_by: Annotated[str, Query(max_length=64)] = "",
    start: Annotated[DateParam, Query(alias="from")] = None,
    end: Annotated[DateParam, Query(alias="to")] = None,
) -> UsageResponse:
    dims = [d for d in group_by.split(",") if d]
    unknown = sorted(set(dims) - set(DIMENSIONS))
    if unknown:
        raise HTTPException(422, f"unknown group_by dimension(s); use {', '.join(DIMENSIONS)}")
    begin, finish = date_range(request.app.state.tz, start, end)
    report = usage_report(
        request.app.state.db, request.app.state.prices,
        start=begin, end=finish, group_by=dims, tz=request.app.state.tz,
    )  # fmt: skip
    return UsageResponse(
        start=begin,
        end=finish,
        group_by=dims,
        rows=[UsageRowOut.of(row) for row in report.rows],
        total=UsageRowOut.of(report.total),
    )
