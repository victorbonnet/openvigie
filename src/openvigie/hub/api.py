"""Collector-facing JSON API: enrolment and (later) ingest. Bodies are untrusted input."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from openvigie.hub.store.credentials import Credentials
from openvigie.model import (
    SCHEMA_VERSION,
    AdapterHealth,
    AgentInstance,
    CodecError,
    Host,
    StatusEvent,
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
    if creds.has_active_token(host.host_id) and not creds.code_bound_to(code, host.host_id):
        raise HTTPException(
            409, "this host is already enrolled; use `openvigie hub hosts rotate` to re-enrol it"
        )
    if not creds.consume_enrol_code(code, host.host_id, now):
        limiter.record()
        raise HTTPException(403, "invalid, expired or already used enrolment code")
    request.app.state.inventory.upsert_host(host, now)
    token = creds.issue_token(host.host_id, now)
    return JSONResponse({"host_id": host.host_id, "token": token}, status_code=201)


INGEST_LIMITS = {"agents": 500, "status_events": 5000, "adapter_health": 64}
KNOWN_SECTIONS = {"schema_version", "host", "sent_at", *INGEST_LIMITS}
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
    ignored = sorted(set(body) - KNOWN_SECTIONS)
    return {
        "schema_version": SCHEMA_VERSION,
        "accepted": accepted,
        "ignored": ignored,
        "next_push_s": NEXT_PUSH_S,
    }
