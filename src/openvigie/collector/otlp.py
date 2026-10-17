"""Local OTLP/HTTP JSON receiver (#64, FR-3): usage straight from agents' own telemetry.

Off by default. When ``[otlp] enabled = true`` the collector listens on ``127.0.0.1:<port>``
(loopback only, never configurable to anything else) for ``/v1/logs``, ``/v1/metrics`` and
``/v1/traces`` in OTLP/HTTP **JSON** (protobuf is refused with 415: no protobuf dependency).
Everything received is untrusted: body size and item caps, strict decoding, and error replies
that never echo the input. An optional shared token (``Authorization: Bearer …``) keeps other
local processes from injecting fake usage.

Mapped today (log events, one per model call):

- Claude Code: ``claude_code.api_request``
  (https://code.claude.com/docs/en/monitoring-usage);
- Codex: ``codex.sse_event`` with ``event.kind = response.completed``
  (``codex-rs/otel/src/events``). As in rollouts, cached input is part of the input count and
  reasoning is part of the output count.

Traces (``/v1/traces``) are passed on to the hub as spans while it exports traces (FR-6), with
an allowlist of attributes: GenAI names that carry no content, ``session.id``, ``error.type``.
Metrics are accepted and ignored.

Source arbitration (``usage_source``), per agent, so nothing is counted twice:

- ``logs``: OTLP usage is ignored.
- ``otlp``: usage only from OTLP; logs still drive detection and sessions.
- ``auto`` (default): logs until OTLP usage is first seen for the agent, then OTLP. The cut-over
  is the newest log record already sent, so neither source counts the overlap twice.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from openvigie import semconv
from openvigie.adapters.base import AdapterState
from openvigie.adapters.usage import int_or_none, parse_time
from openvigie.model import AgentInstance, Span, SpanStatus, UsageQuality, UsageRecord, stable_id

log = logging.getLogger(__name__)

BIND = "127.0.0.1"
PATHS = ("/v1/logs", "/v1/metrics", "/v1/traces")
MAX_BODY = 4 * 1024 * 1024
MAX_RECORDS = 5000  # log records per request
MAX_ATTRIBUTES = 128
MAX_QUEUE = 20_000
MAX_STR = 256
MAX_SPANS = 5000  # per request, and queued
SPAN_KEEP = {"session.id", "error.type", "conversation.id"}
SPAN_DROP_WORDS = ("prompt", "completion", "message", "argument", "result", "content", "input.",
                   "output.")  # fmt: skip
STATUS = {0: SpanStatus.UNSET, 1: SpanStatus.OK, 2: SpanStatus.ERROR}
HEX = frozenset("0123456789abcdef")

Scalar = str | int | float | bool


@dataclass(frozen=True)
class OtlpUsage:
    """One model call as reported by an agent's telemetry."""

    agent_type: str
    timestamp: datetime
    provider: str
    request_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None


# --- decoding ----------------------------------------------------------------------------------


def attributes(items: Any) -> dict[str, Scalar]:
    """OTLP ``KeyValue`` list → dict of scalars; anything else is skipped."""
    out: dict[str, Scalar] = {}
    if not isinstance(items, list):
        return out
    for item in items[:MAX_ATTRIBUTES]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        value = item.get("value")
        if not isinstance(value, dict) or len(item["key"]) > MAX_STR:
            continue
        if isinstance(v := value.get("stringValue"), str):
            out[item["key"]] = v[:MAX_STR]
        elif isinstance(v := value.get("boolValue"), bool):
            out[item["key"]] = v
        elif (n := _int(value.get("intValue"))) is not None:
            out[item["key"]] = n
        elif isinstance(v := value.get("doubleValue"), int | float) and not isinstance(v, bool):
            out[item["key"]] = float(v)
    return out


def _int(value: Any) -> int | None:
    """OTLP JSON encodes 64-bit ints as strings; agents also send counts as string attributes."""
    if isinstance(value, str) and value.isdigit() and len(value) <= 19:
        value = int(value)
    return int_or_none(value)


def _nanos(value: Any) -> datetime | None:
    """Unix nanoseconds (a string in OTLP JSON; above 2**53, so not a token count)."""
    if isinstance(value, str) and value.isdigit() and len(value) <= 19:
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**63:
        return None
    return datetime.fromtimestamp(value // 1000 / 1e6, UTC)


def _string(value: Scalar | None) -> str | None:
    return value if isinstance(value, str) and value else None


def claude_code_event(attrs: dict[str, Scalar], record: dict[str, Any]) -> OtlpUsage | None:
    body = record.get("body")
    body_text = body.get("stringValue") if isinstance(body, dict) else None
    if attrs.get("event.name") != "api_request" and body_text != "claude_code.api_request":
        return None
    when = (
        parse_time(attrs.get("event.timestamp"))
        or _nanos(record.get("timeUnixNano"))
        or _nanos(record.get("observedTimeUnixNano"))
    )
    if when is None:
        return None
    return OtlpUsage(
        agent_type="claude-code",
        timestamp=when,
        provider="anthropic",
        request_id=_string(attrs.get("request_id")),
        session_id=_string(attrs.get("session.id")),
        model=_string(attrs.get("model")),
        input_tokens=_int(attrs.get("input_tokens")),
        output_tokens=_int(attrs.get("output_tokens")),
        cache_read_input_tokens=_int(attrs.get("cache_read_tokens")),
        cache_creation_input_tokens=_int(attrs.get("cache_creation_tokens")),
    )


def codex_event(attrs: dict[str, Scalar], record: dict[str, Any]) -> OtlpUsage | None:
    if attrs.get("event.name") != "codex.sse_event":
        return None
    if attrs.get("event.kind") != "response.completed" or "error.message" in attrs:
        return None
    when = parse_time(attrs.get("event.timestamp")) or _nanos(record.get("timeUnixNano"))
    if when is None:
        return None
    return OtlpUsage(
        agent_type="codex",
        timestamp=when,
        provider="openai",
        session_id=_string(attrs.get("conversation.id")),
        model=_string(attrs.get("model")),
        input_tokens=_int(attrs.get("input_token_count")),
        output_tokens=_int(attrs.get("output_token_count")),
        cache_read_input_tokens=_int(attrs.get("cached_token_count")),
        cache_creation_input_tokens=_int(attrs.get("cache_write_token_count")),
        reasoning_tokens=_int(attrs.get("reasoning_token_count")),
    )


Mapper = Callable[[dict[str, Scalar], dict[str, Any]], OtlpUsage | None]
# by service.name; Codex's varies by front end (codex_cli_rs, codex_exec, …), so events whose
# name starts with "codex." are recognised whatever the service
LOG_MAPPERS: dict[str, Mapper] = {"claude-code": claude_code_event}


def parse_logs(payload: Any) -> list[OtlpUsage]:
    """Usage from an ``ExportLogsServiceRequest``; raises ValueError if it has too many records."""
    found: list[OtlpUsage] = []
    seen = 0
    resources = payload.get("resourceLogs") if isinstance(payload, dict) else None
    for resource in resources if isinstance(resources, list) else []:
        if not isinstance(resource, dict):
            continue
        res = resource.get("resource")
        service = attributes(res.get("attributes") if isinstance(res, dict) else None)
        mapper = LOG_MAPPERS.get(str(service.get("service.name")))
        scopes = resource.get("scopeLogs")
        for scope in scopes if isinstance(scopes, list) else []:
            records = scope.get("logRecords") if isinstance(scope, dict) else None
            for record in records if isinstance(records, list) else []:
                seen += 1
                if seen > MAX_RECORDS:
                    raise ValueError("too many log records")
                if not isinstance(record, dict):
                    continue
                attrs = attributes(record.get("attributes"))
                event = attrs.get("event.name")
                chosen = (
                    codex_event if isinstance(event, str) and event.startswith("codex.") else mapper
                )
                if chosen is None:
                    continue
                usage = chosen(attrs, record)
                if usage is not None:
                    found.append(usage)
    return found


def _agent_type(service: Scalar | None) -> str | None:
    if service == "claude-code":
        return "claude-code"
    if isinstance(service, str) and service.startswith("codex"):
        return "codex"
    return None


def _hex(value: Any, length: int) -> str | None:
    if isinstance(value, str) and len(value) == length and set(value.lower()) <= HEX:
        return value.lower()
    return None


def _span_attributes(attrs: dict[str, Scalar]) -> dict[str, Scalar]:
    """Only attributes that can't hold content: agents may put prompts in their own spans."""
    return {
        k: v
        for k, v in attrs.items()
        if k in SPAN_KEEP
        or (k.startswith("gen_ai.") and k not in semconv.CONTENT_ATTRIBUTES
            and not any(word in k for word in SPAN_DROP_WORDS))
    }  # fmt: skip


def parse_traces(payload: Any) -> list[tuple[str, Span]]:
    """(agent type, span) pairs from an ``ExportTraceServiceRequest`` of a known agent."""
    found: list[tuple[str, Span]] = []
    seen = 0
    resources = payload.get("resourceSpans") if isinstance(payload, dict) else None
    for resource in resources if isinstance(resources, list) else []:
        if not isinstance(resource, dict):
            continue
        res = resource.get("resource")
        agent_type = _agent_type(
            attributes(res.get("attributes") if isinstance(res, dict) else None).get("service.name")
        )
        scopes = resource.get("scopeSpans")
        for scope in scopes if isinstance(scopes, list) else []:
            spans = scope.get("spans") if isinstance(scope, dict) else None
            for raw in spans if isinstance(spans, list) else []:
                seen += 1
                if seen > MAX_SPANS:
                    raise ValueError("too many spans")
                if agent_type is None or not isinstance(raw, dict):
                    continue
                trace, span = _hex(raw.get("traceId"), 32), _hex(raw.get("spanId"), 16)
                start, name = _nanos(raw.get("startTimeUnixNano")), raw.get("name")
                if not trace or not span or not start or not isinstance(name, str):
                    continue
                status = raw.get("status")
                code = _int(status.get("code")) if isinstance(status, dict) else None
                found.append((agent_type, Span(
                    trace_id=trace,
                    span_id=span,
                    name=name[:MAX_STR],
                    start_time=start,
                    end_time=_nanos(raw.get("endTimeUnixNano")),
                    parent_span_id=_hex(raw.get("parentSpanId"), 16),
                    status=STATUS.get(code or 0, SpanStatus.UNSET),
                    attributes=_span_attributes(attributes(raw.get("attributes"))),
                )))  # fmt: skip
    return found


# --- receiver ----------------------------------------------------------------------------------


class OtlpReceiver:
    def __init__(self, port: int = 4318, token: str | None = None) -> None:
        self.port = port
        self._token = token
        self._queue: deque[OtlpUsage] = deque(maxlen=MAX_QUEUE)
        self._spans: deque[tuple[str, Span]] = deque(maxlen=MAX_SPANS)
        self._lock = threading.Lock()
        self.dropped = 0
        self.received = 0
        self.server: ThreadingHTTPServer | None = None

    def __repr__(self) -> str:
        return f"OtlpReceiver(port={self.port})"

    def handle(self, path: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        """Process one request; returns (status, JSON body). Never echoes the input."""
        headers = {k.lower(): v for k, v in headers.items()}
        if path not in PATHS:
            return 404, b'{"message": "unknown path"}'
        if self._token is not None:
            given = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given.encode(), self._token.encode()):
                return 401, b'{"message": "missing or wrong token"}'
        if headers.get("content-type", "").split(";")[0].strip() != "application/json":
            return 415, b'{"message": "only OTLP/HTTP JSON is supported (http/json)"}'
        if len(body) > MAX_BODY:
            return 413, b'{"message": "request too large"}'
        try:
            payload = json.loads(body)
            usage = parse_logs(payload) if path == "/v1/logs" else []
            spans = parse_traces(payload) if path == "/v1/traces" else []
        except ValueError:
            return 400, b'{"message": "invalid OTLP JSON"}'
        with self._lock:
            self._spans.extend(spans)
            if len(self._queue) + len(usage) > MAX_QUEUE:
                self.dropped += len(self._queue) + len(usage) - MAX_QUEUE
            self._queue.extend(usage)
            self.received += len(usage)
        return 200, b"{}"

    def drain(self) -> list[OtlpUsage]:
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    def drain_spans(self) -> list[tuple[str, Span]]:
        with self._lock:
            items = list(self._spans)
            self._spans.clear()
        return items

    def start(self) -> None:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    status, reply = 413, b'{"message": "request too large"}'
                else:
                    body = self.rfile.read(length)
                    status, reply = receiver.handle(self.path, dict(self.headers), body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass  # request lines could carry anything; stay quiet

        self.server = ThreadingHTTPServer((BIND, self.port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="otlp", daemon=True).start()
        log.info("OTLP receiver listening on http://%s:%d", BIND, self.port)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# --- arbitration -------------------------------------------------------------------------------


def to_record(agent: AgentInstance, usage: OtlpUsage) -> UsageRecord:
    key = usage.request_id or f"{usage.session_id}:{usage.timestamp.isoformat()}"
    known = usage.input_tokens is not None and usage.output_tokens is not None
    return UsageRecord(
        record_id=stable_id(agent.agent_id, "otlp", key),
        agent_id=agent.agent_id,
        timestamp=usage.timestamp,
        session_id=usage.session_id,
        provider=usage.provider,
        response_model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        quality=UsageQuality.EXACT if known else UsageQuality.UNKNOWN,
    )


class Arbiter:
    """Chooses, per agent, which source's usage records are sent (see module docstring)."""

    def __init__(self, mode: str = "auto") -> None:
        self.mode = mode

    @staticmethod
    def _key(agent: AgentInstance) -> str:
        return "usage-source:" + agent.agent_id

    def otlp(
        self, agent: AgentInstance, items: list[OtlpUsage], state: AdapterState
    ) -> list[UsageRecord]:
        if self.mode == "logs" or not items:
            return []
        if self.mode == "otlp":
            return [to_record(agent, i) for i in items]
        info = dict(state.get(self._key(agent)) or {})
        since = parse_time(info.get("since"))
        if since is None:
            # cut over after the newest log record already sent; before any, keep all of OTLP
            sent = parse_time(info.get("logs_until"))
            since = sent or min(i.timestamp for i in items) - timedelta(microseconds=1)
            info["since"] = since.isoformat()
            state.stage(self._key(agent), info)
            log.info("%s: usage now comes from OTLP (after %s)", agent.agent_type, since)
        return [to_record(agent, i) for i in items if i.timestamp > since]

    def logs(
        self, agent: AgentInstance, records: list[UsageRecord], state: AdapterState
    ) -> list[UsageRecord]:
        if self.mode == "otlp":
            return []
        info = dict(state.get(self._key(agent)) or {})
        since = parse_time(info.get("since"))
        if self.mode == "auto" and since is not None:
            records = [r for r in records if r.timestamp <= since]
        if records:
            newest = max(r.timestamp for r in records)
            sent = parse_time(info.get("logs_until"))
            info["logs_until"] = max(newest, sent or newest).isoformat()
            state.stage(self._key(agent), info)
        return records
