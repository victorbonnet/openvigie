"""Claude Code transcripts → OTel GenAI spans (FR-6).

One trace per session:

- ``invoke_agent claude-code``: root span, from the first to the last transcript entry;
- ``invoke_agent subagent``: one per subagent transcript (``<session>/subagents/*.jsonl``);
- ``chat <model>``: one per API response, from the entry before it to its last line;
- ``execute_tool <name>``: one per ``tool_use`` / ``tool_result`` pair.

Transcripts are read incrementally, so a span may be emitted again on a later tick with a later
end time or a status; its id stays the same and backends upsert it. What must survive between
ticks (session bounds, the open response, tools waiting for a result) is kept in the adapter
state under ``span:<file key>`` and committed with the file cursor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from openvigie import semconv
from openvigie.adapters.base import AdapterState
from openvigie.adapters.spans import content, span_id, trace_id
from openvigie.adapters.usage import parse_time
from openvigie.model import AgentInstance, AttrValue, Span, SpanStatus, UsageRecord

AGENT_NAME = "claude-code"
MAX_PENDING_TOOLS = 256
MAX_NAME = 128


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _short(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= MAX_NAME else None


class TranscriptSpans:
    """Builds the spans of one transcript file across ticks."""

    def __init__(
        self, agent: AgentInstance, key: str, state: AdapterState, *, export_content: bool
    ) -> None:
        self.agent = agent
        self.state = state
        self.state_key = "span:" + key
        self.export_content = export_content
        saved = state.get(self.state_key) or {}
        self.session: str | None = _short(saved.get("session"))
        self.subagent: str | None = _short(saved.get("subagent"))
        self.first = parse_time(saved.get("first"))
        self.last = parse_time(saved.get("last"))
        self.prev = parse_time(saved.get("prev"))  # previous entry, in file order
        message = saved.get("message")
        self.message: list[Any] = message if isinstance(message, list) and len(message) == 2 else []
        tools = saved.get("tools")
        self.tools: dict[str, list[Any]] = tools if isinstance(tools, dict) else {}
        self.spans: dict[str, Span] = {}

    # --- ids -----------------------------------------------------------------------------------

    @property
    def trace(self) -> str:
        return trace_id(self.agent.agent_id, self.session or "")

    @property
    def root(self) -> str:
        return span_id(self.trace, "root")

    @property
    def parent(self) -> str:
        return span_id(self.trace, "subagent:" + self.subagent) if self.subagent else self.root

    # --- entries -------------------------------------------------------------------------------

    def add(self, entry: object, usage: UsageRecord | None) -> None:
        if not isinstance(entry, dict):
            return
        when = parse_time(entry.get("timestamp"))
        if when is None:
            return
        self.session = self.session or _short(entry.get("sessionId"))
        if entry.get("isSidechain") is True:
            self.subagent = self.subagent or _short(entry.get("agentId"))
        if self.session is None:
            return
        self.first = min(self.first, when) if self.first else when
        self.last = max(self.last, when) if self.last else when
        message = entry.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        blocks = blocks if isinstance(blocks, list) else []
        if usage is not None and isinstance(message, dict):
            self._chat(message, usage, when, blocks)
        elif entry.get("type") == "user":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self._tool_result(block, when)
        self.prev = when

    def _chat(
        self, message: dict[str, Any], usage: UsageRecord, when: datetime, blocks: list[Any]
    ) -> None:
        message_id = str(message.get("id"))
        if not self.message or self.message[0] != message_id:
            start = self.prev if self.prev and self.prev <= when else when
            self.message = [message_id, _iso(start)]
        sid = span_id(self.trace, "chat:" + message_id)
        model = usage.response_model or usage.request_model or "unknown"
        attributes: dict[str, AttrValue] = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_CHAT,
            semconv.GEN_AI_RESPONSE_ID: message_id[:MAX_NAME],
            **usage.semconv_attributes(),
        }
        previous = self.spans.get(sid)
        if self.export_content:
            texts = [
                b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            ]
            text = "".join(t for t in texts if isinstance(t, str))
            earlier = previous.attributes.get(semconv.GEN_AI_OUTPUT_MESSAGES) if previous else None
            if text or earlier:
                attributes[semconv.GEN_AI_OUTPUT_MESSAGES] = content(
                    (str(earlier) if earlier else "") + text
                )
        self.spans[sid] = Span(
            trace_id=self.trace,
            span_id=sid,
            name=f"chat {model}",
            start_time=parse_time(self.message[1]) or when,
            end_time=when,
            parent_span_id=self.parent,
            agent_id=self.agent.agent_id,
            attributes=attributes,
        )
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                self._tool_use(block, when, sid)

    def _tool_use(self, block: dict[str, Any], when: datetime, parent: str) -> None:
        call_id, name = _short(block.get("id")), _short(block.get("name"))
        if call_id is None or name is None:
            return
        if len(self.tools) >= MAX_PENDING_TOOLS:  # results that never came (interrupted turns)
            self.tools.pop(next(iter(self.tools)))
        self.tools[call_id] = [name, _iso(when), parent]
        extra: dict[str, AttrValue] = {}
        if self.export_content and "input" in block:
            extra[semconv.GEN_AI_TOOL_CALL_ARGUMENTS] = content(block["input"])
        self._tool_span(call_id, when, False, SpanStatus.UNSET, extra)

    def _tool_result(self, block: dict[str, Any], when: datetime) -> None:
        call_id = _short(block.get("tool_use_id"))
        if call_id is None or call_id not in self.tools:
            return
        failed = block.get("is_error") is True  # the API treats a missing is_error as success
        extra: dict[str, AttrValue] = {}
        if failed:
            extra[semconv.ERROR_TYPE] = "tool_error"
        if self.export_content and "content" in block:
            extra[semconv.GEN_AI_TOOL_CALL_RESULT] = content(block["content"])
        status = SpanStatus.ERROR if failed else SpanStatus.OK
        self._tool_span(call_id, when, True, status, extra)
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
        attributes: dict[str, AttrValue] = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_EXECUTE_TOOL,
            semconv.GEN_AI_TOOL_NAME: name,
            semconv.GEN_AI_TOOL_CALL_ID: call_id,
        }
        previous = self.spans.get(sid)
        if previous:  # keep the arguments seen earlier in this tick
            attributes = {**previous.attributes, **attributes}
        self.spans[sid] = Span(
            trace_id=self.trace,
            span_id=sid,
            name=f"execute_tool {name}",
            start_time=parse_time(started) or when,
            end_time=when if finished else None,
            parent_span_id=parent,
            agent_id=self.agent.agent_id,
            status=status,
            attributes={**attributes, **extra},
        )

    # --- end of tick ---------------------------------------------------------------------------

    def finish(self) -> list[Span]:
        """Spans touched this tick, plus the session (or subagent) span; stages the state."""
        if self.session is None or self.first is None:
            return []
        common: dict[str, AttrValue] = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OP_INVOKE_AGENT,
            semconv.GEN_AI_PROVIDER_NAME: "anthropic",
            semconv.SESSION_ID: self.session,
            semconv.GEN_AI_CONVERSATION_ID: self.session,
            semconv.OPENVIGIE_AGENT_ID: self.agent.agent_id,
            semconv.OPENVIGIE_AGENT_TYPE: self.agent.agent_type,
        }
        if self.subagent:
            name, parent = "subagent", self.root
            common[semconv.GEN_AI_AGENT_ID] = self.subagent
        else:
            name, parent = AGENT_NAME, None
        own = Span(
            trace_id=self.trace,
            span_id=self.parent,
            name=f"invoke_agent {name}",
            start_time=self.first,
            end_time=self.last,
            parent_span_id=parent,
            agent_id=self.agent.agent_id,
            attributes={**common, semconv.GEN_AI_AGENT_NAME: name},
        )
        self.state.stage(
            self.state_key,
            {
                "session": self.session,
                "subagent": self.subagent,
                "first": _iso(self.first),
                "last": _iso(self.last),
                "prev": _iso(self.prev),
                "message": self.message,
                "tools": self.tools,
            },
        )
        return [own, *self.spans.values()]
