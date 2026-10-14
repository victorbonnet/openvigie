"""Trace export to Langfuse over OTLP/HTTP JSON (#62, FR-6).

Only active when ``[langfuse]`` is configured; otherwise the hub opens no outbound connection.
Collectors push spans into the export queue; a background task sends them in batches to
``<host>/api/public/otel/v1/traces`` (Langfuse accepts OTLP over HTTP as JSON or protobuf; JSON
needs no extra dependency) with Basic auth ``public_key:secret_key``.

Defense in depth: every string attribute passes ``redact()`` again here, and content
attributes are dropped unless ``export_content`` is on, whatever the collector sent.

Failures: 5xx, 429 and network errors back off per span (up to ``MAX_ATTEMPTS``, then the span
is dropped and counted); 401/403 pause the exporter (a configuration problem, nothing is
dropped); other 4xx drop the batch at once, since resending it would fail the same way.
Redirects are never followed, so the auth header only ever goes to the configured host.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI

from openvigie import __version__, semconv
from openvigie.config import LangfuseSettings
from openvigie.hub.store.db import Database
from openvigie.hub.store.spans import QueuedSpan, SpanQueue
from openvigie.model import AttrValue, Span, SpanStatus
from openvigie.redact import redact

log = logging.getLogger(__name__)

ENDPOINT = "/api/public/otel/v1/traces"
LANGFUSE_SESSION_ID = "langfuse.session.id"
BATCH = 200
INTERVAL_S = 5.0
TIMEOUT_S = 10.0
MAX_ATTEMPTS = 8
FIRST_BACKOFF = timedelta(seconds=10)
MAX_BACKOFF = timedelta(minutes=10)
AUTH_PAUSE = timedelta(minutes=10)
MAX_RESPONSE = 64 * 1024

KIND_INTERNAL, KIND_CLIENT = 1, 3
STATUS_CODES = {SpanStatus.UNSET: 0, SpanStatus.OK: 1, SpanStatus.ERROR: 2}

# (url, body, headers, timeout) -> HTTP status; raises OSError on network failure
Transport = Callable[[str, bytes, dict[str, str], float], int]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError with the 3xx status


def urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - scheme checked in config
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(MAX_RESPONSE)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


# --- OTLP JSON ---------------------------------------------------------------------------------


def _nanos(when: datetime) -> str:
    return str(int(when.timestamp()) * 1_000_000_000 + when.microsecond * 1000)


def _any_value(value: AttrValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": value}


def _attributes(attrs: dict[str, AttrValue], export_content: bool) -> list[dict[str, Any]]:
    out = []
    for key, value in attrs.items():
        if key in semconv.CONTENT_ATTRIBUTES and not export_content:
            continue
        out.append({"key": key, "value": _any_value(redact(value) if isinstance(value, str)
                                                   else value)})  # fmt: skip
    session = attrs.get(semconv.SESSION_ID)
    if isinstance(session, str):  # groups the trace under a Langfuse session
        out.append({"key": LANGFUSE_SESSION_ID, "value": _any_value(redact(session))})
    return out


def otlp_span(span: Span, *, export_content: bool) -> dict[str, Any]:
    chat = span.attributes.get(semconv.GEN_AI_OPERATION_NAME) == semconv.OP_CHAT
    out: dict[str, Any] = {
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": redact(span.name),
        "kind": KIND_CLIENT if chat else KIND_INTERNAL,
        "startTimeUnixNano": _nanos(span.start_time),
        # still-open spans (a tool without its result yet) go out as instants and are
        # re-exported, same ids, once the collector sends them closed
        "endTimeUnixNano": _nanos(span.end_time or span.start_time),
        "attributes": _attributes(dict(span.attributes), export_content),
        "status": {"code": STATUS_CODES[span.status]},
    }
    if span.parent_span_id:
        out["parentSpanId"] = span.parent_span_id
    return out


def otlp_payload(batch: list[QueuedSpan], *, export_content: bool) -> dict[str, Any]:
    by_host: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for queued in batch:
        by_host[queued.host_id].append(otlp_span(queued.span, export_content=export_content))
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "openvigie"}},
                        {"key": "host.id", "value": {"stringValue": host_id}},
                    ]
                },
                "scopeSpans": [
                    {"scope": {"name": "openvigie", "version": __version__}, "spans": spans}
                ],
            }
            for host_id, spans in by_host.items()
        ]
    }


# --- exporter ----------------------------------------------------------------------------------


class LangfuseExporter:
    def __init__(
        self,
        settings: LangfuseSettings,
        db: Database,
        *,
        queue_hours: int = 24,
        transport: Transport = urllib_post,
    ) -> None:
        self.settings = settings
        self.url = settings.host.rstrip("/") + ENDPOINT
        credentials = f"{settings.public_key}:{settings.secret_key()}".encode()
        self._headers = {
            "Authorization": "Basic " + base64.b64encode(credentials).decode(),
            "Content-Type": "application/json",
            "User-Agent": f"openvigie/{__version__}",
        }
        self.queue = SpanQueue(db)
        self.queue_hours = queue_hours
        self.transport = transport
        self.paused_until: datetime | None = None
        self.last_error: str | None = None
        self.last_export_at: datetime | None = None

    def __repr__(self) -> str:  # never show the auth header
        return f"LangfuseExporter(url={self.url!r})"

    def run_once(self, now: datetime | None = None) -> int:
        """Send what is due; returns the number of spans Langfuse accepted."""
        now = now or datetime.now(UTC)
        self.queue.prune(now - timedelta(hours=self.queue_hours))
        if self.paused_until and now < self.paused_until:
            return 0
        sent = 0
        while batch := self.queue.due(now, BATCH):
            if not self._send(batch, now):
                break
            sent += len(batch)
        return sent

    def _send(self, batch: list[QueuedSpan], now: datetime) -> bool:
        payload = otlp_payload(batch, export_content=self.settings.export_content)
        body = json.dumps(payload, separators=(",", ":")).encode()
        reason = ""
        try:
            status: int | None = self.transport(self.url, body, dict(self._headers), TIMEOUT_S)
        except (OSError, ValueError) as exc:
            status, reason = None, f"cannot reach Langfuse ({type(exc).__name__})"
        if status is not None and 200 <= status < 300:
            self.queue.mark_exported(batch, now)
            self.last_error, self.last_export_at, self.paused_until = None, now, None
            return True
        if status in (401, 403):
            self.paused_until = now + AUTH_PAUSE
            self._fail(f"Langfuse rejected the API keys (HTTP {status}); export paused")
            return False
        if status is None or status == 429 or status >= 500:
            self._back_off(batch, now)
            self._fail(reason if status is None else f"Langfuse unavailable (HTTP {status})")
            return False
        self.queue.drop(batch)
        self._fail(f"Langfuse refused a batch (HTTP {status}); {len(batch)} spans dropped")
        return False

    def _back_off(self, batch: list[QueuedSpan], now: datetime) -> None:
        expired = [q for q in batch if q.attempts + 1 >= MAX_ATTEMPTS]
        retry = [q for q in batch if q.attempts + 1 < MAX_ATTEMPTS]
        if expired:
            self.queue.drop(expired)
        if retry:
            delay = min(FIRST_BACKOFF * 2 ** min(q.attempts for q in retry), MAX_BACKOFF)
            self.queue.retry_later(retry, now + delay)

    def _fail(self, message: str) -> None:
        if message != self.last_error:
            log.warning("trace export: %s", message)
        self.last_error = message


@asynccontextmanager
async def export_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the exporter every few seconds in a worker thread, when one is configured."""
    exporter: LangfuseExporter | None = app.state.exporter
    if exporter is None:
        yield
        return

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(exporter.run_once)
            except Exception:
                log.exception("trace export run failed")
            await asyncio.sleep(INTERVAL_S)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
