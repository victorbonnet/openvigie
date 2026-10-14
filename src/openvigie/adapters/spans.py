"""Shared helpers for building OTel GenAI spans from agent transcripts (FR-6).

Ids are deterministic, so a re-read yields identical spans and the hub can build trace links
without asking the backend: ``trace_id = stable_id(agent_id, session_id)`` (16 bytes) and
``span_id = stable_id(trace_id, key)[:16]`` (8 bytes).

Content (prompts, outputs, tool arguments and results) is left out unless
``TraceOptions.export_content`` is on; then it is redacted first and truncated.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, ClassVar

from openvigie import semconv
from openvigie.adapters.base import AdapterState
from openvigie.adapters.usage import parse_time
from openvigie.model import AgentInstance, AttrValue, Span, SpanStatus, UsageRecord, stable_id
from openvigie.redact import redact, redact_obj

CONTENT_MAX_CHARS = 4096
MAX_NAME = 128
MAX_PENDING_TOOLS = 256
TRUNCATED = "…[truncated]"


def trace_id(agent_id: str, session_id: str) -> str:
    return stable_id(agent_id, session_id)


def span_id(trace: str, key: str) -> str:
    return stable_id(trace, key)[:16]


def content(value: Any) -> str:
    """Redacted, truncated text for a content attribute (JSON for non-strings)."""
    if isinstance(value, str):
        text = redact(value)
    else:
        text = json.dumps(redact_obj(value), ensure_ascii=False, default=str)
    if len(text) > CONTENT_MAX_CHARS:
        text = text[: CONTENT_MAX_CHARS - len(TRUNCATED)] + TRUNCATED  # fits the wire limit
    return text


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def short(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_NAME else None


class SessionSpans:
    """The spans of one session file, built across ticks.

    Files are read incrementally, so a span may be emitted again on a later tick with a later end
    time or a status; its id stays the same and backends upsert it. What must survive between
    ticks (session bounds, tool calls waiting for a result, plus whatever a subclass adds in
    ``extra_state``) is staged under ``span:<file key>`` and committed with the file cursor.
    """

    agent_name: ClassVar[str]

    def __init__(
        self,
        agent: AgentInstance,
        key: str,
        state: AdapterState,
        *,
        provider: str,
        export_content: bool,
    ) -> None:
        self.agent = agent
        self.state = state
        self.state_key = "span:" + key
        self.provider = provider
        self.export_content = export_content
        self.saved: dict[str, Any] = state.get(self.state_key) or {}
        self.session = short(self.saved.get("session"))
        self.first = parse_time(self.saved.get("first"))
        self.last = parse_time(self.saved.get("last"))
        tools = self.saved.get("tools")
        # call id → [tool name, start time, parent span id]
        self.tools: dict[str, list[Any]] = tools if isinstance(tools, dict) else {}
        self.spans: dict[str, Span] = {}

    @property
    def trace(self) -> str:
        return trace_id(self.agent.agent_id, self.session or "")

    @property
    def root(self) -> str:
        return span_id(self.trace, "root")

    @property
    def parent(self) -> str:
        """Parent of the chat spans of this file."""
        return self.root

    def touch(self, when: datetime) -> None:
        self.first = min(self.first, when) if self.first else when
        self.last = max(self.last, when) if self.last else when

    def chat(
        self,
        key: str,
        usage: UsageRecord,
        start: datetime,
        end: datetime,
        *,
        response_id: str | None = None,
        output_text: str = "",
    ) -> str:
        sid = span_id(self.trace, "chat:" + key)
        model = usage.response_model or usage.request_model or "unknown"
        attributes: dict[str, AttrValue] = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_CHAT,
            **usage.semconv_attributes(),
        }
        if response_id:
            attributes[semconv.GEN_AI_RESPONSE_ID] = response_id[:MAX_NAME]
        previous = self.spans.get(sid)
        earlier = previous.attributes.get(semconv.GEN_AI_OUTPUT_MESSAGES) if previous else None
        if self.export_content and (output_text or earlier):
            text = (str(earlier) if earlier else "") + output_text
            attributes[semconv.GEN_AI_OUTPUT_MESSAGES] = content(text)
        self.spans[sid] = Span(
            trace_id=self.trace,
            span_id=sid,
            name=f"chat {model}",
            start_time=min(start, end),
            end_time=end,
            parent_span_id=self.parent,
            agent_id=self.agent.agent_id,
            attributes=attributes,
        )
        return sid

    def tool_started(
        self, call_id: str, name: str, when: datetime, parent: str, arguments: Any = None
    ) -> None:
        if len(self.tools) >= MAX_PENDING_TOOLS:  # results that never came (interrupted turns)
            self.tools.pop(next(iter(self.tools)))
        self.tools[call_id] = [name, iso(when), parent]
        extra: dict[str, AttrValue] = {}
        if self.export_content and arguments is not None:
            extra[semconv.GEN_AI_TOOL_CALL_ARGUMENTS] = content(arguments)
        self._tool_span(call_id, when, False, SpanStatus.UNSET, extra)

    def tool_finished(
        self,
        call_id: str,
        when: datetime,
        *,
        error: str | None,
        status: SpanStatus | None = None,
        result: Any = None,
        extra: dict[str, AttrValue] | None = None,
    ) -> None:
        """Close a pending tool span.

        ``error`` is an ``error.type`` value, or None on success; pass ``status=UNSET`` when the
        outcome can't be told.
        """
        if call_id not in self.tools:
            return
        attributes = dict(extra or {})
        if error:
            attributes[semconv.ERROR_TYPE] = error
        if self.export_content and result is not None:
            attributes[semconv.GEN_AI_TOOL_CALL_RESULT] = content(result)
        if status is None:
            status = SpanStatus.ERROR if error else SpanStatus.OK
        self._tool_span(call_id, when, True, status, attributes)
        del self.tools[call_id]

    def _tool_span(
        self,
        call_id: str,
        when: datetime,
        finished: bool,
        status: SpanStatus,
        extra: dict[str, AttrValue],
    ) -> None:
        name, started, parent = self.tools[call_id]
        sid = span_id(self.trace, "tool:" + call_id)
        previous = self.spans.get(sid)
        attributes: dict[str, AttrValue] = {
            **(previous.attributes if previous else {}),  # arguments seen earlier this tick
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_EXECUTE_TOOL,
            semconv.GEN_AI_TOOL_NAME: name,
            semconv.GEN_AI_TOOL_CALL_ID: call_id,
            **extra,
        }
        start = parse_time(started) or when
        self.spans[sid] = Span(
            trace_id=self.trace,
            span_id=sid,
            name=f"execute_tool {name}",
            start_time=min(start, when),
            end_time=when if finished else None,
            parent_span_id=parent,
            agent_id=self.agent.agent_id,
            status=status,
            attributes=attributes,
        )

    def own_span(self, first: datetime, last: datetime) -> Span:
        """The span this file stands for: the session's ``invoke_agent`` root by default."""
        return Span(
            trace_id=self.trace,
            span_id=self.root,
            name=f"invoke_agent {self.agent_name}",
            start_time=first,
            end_time=last,
            agent_id=self.agent.agent_id,
            attributes={**self.session_attributes(), semconv.GEN_AI_AGENT_NAME: self.agent_name},
        )

    def session_attributes(self) -> dict[str, AttrValue]:
        return {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
            semconv.GEN_AI_PROVIDER_NAME: self.provider,
            semconv.SESSION_ID: self.session or "",
            semconv.GEN_AI_CONVERSATION_ID: self.session or "",
            semconv.OPENVIGIE_AGENT_ID: self.agent.agent_id,
            semconv.OPENVIGIE_AGENT_TYPE: self.agent.agent_type,
        }

    def extra_state(self) -> dict[str, Any]:
        return {}

    def finish(self) -> list[Span]:
        """Spans touched this tick plus this file's own span; stages the state."""
        if self.session is None or self.first is None or self.last is None:
            return []
        self.state.stage(
            self.state_key,
            {
                "session": self.session,
                "first": iso(self.first),
                "last": iso(self.last),
                "tools": self.tools,
                **self.extra_state(),
            },
        )
        return [self.own_span(self.first, self.last), *self.spans.values()]
