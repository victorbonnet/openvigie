"""Claude Code transcripts → OTel GenAI spans (FR-6).

One trace per session:

- ``invoke_agent claude-code``: root span, from the first to the last transcript entry;
- ``invoke_agent subagent``: one per subagent transcript (``<session>/subagents/*.jsonl``);
- ``chat <model>``: one per API response, from the entry before it to its last line;
- ``execute_tool <name>``: one per ``tool_use`` / ``tool_result`` pair.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from openvigie import semconv
from openvigie.adapters.spans import SessionSpans, iso, short, span_id
from openvigie.adapters.usage import parse_time
from openvigie.model import Span, UsageRecord


class TranscriptSpans(SessionSpans):
    """Spans of one Claude Code transcript file (main session or subagent)."""

    agent_name = "claude-code"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, provider="anthropic", **kwargs)
        self.subagent = short(self.saved.get("subagent"))
        self.prev = parse_time(self.saved.get("prev"))  # previous entry, in file order
        message = self.saved.get("message")  # [message id, start] of the response being read
        self.message: list[Any] = message if isinstance(message, list) and len(message) == 2 else []

    @property
    def parent(self) -> str:
        return span_id(self.trace, "subagent:" + self.subagent) if self.subagent else self.root

    def add(self, entry: object, usage: UsageRecord | None) -> None:
        if not isinstance(entry, dict):
            return
        when = parse_time(entry.get("timestamp"))
        if when is None:
            return
        self.session = self.session or short(entry.get("sessionId"))
        if entry.get("isSidechain") is True:
            self.subagent = self.subagent or short(entry.get("agentId"))
        if self.session is None:
            return
        self.touch(when)
        message = entry.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
        if usage is not None and isinstance(message, dict):
            self._response(str(message.get("id")), usage, when, blocks)
        elif entry.get("type") == "user":
            for block in blocks:
                call_id = short(block.get("tool_use_id"))
                if block.get("type") == "tool_result" and call_id:
                    failed = block.get("is_error") is True  # missing is_error means success
                    self.tool_finished(
                        call_id, when, error="tool_error" if failed else None,
                        result=block.get("content"),
                    )  # fmt: skip
        self.prev = when

    def _response(
        self, message_id: str, usage: UsageRecord, when: datetime, blocks: list[dict[str, Any]]
    ) -> None:
        if not self.message or self.message[0] != message_id:
            start = self.prev if self.prev and self.prev <= when else when
            self.message = [message_id, iso(start)]
        text = "".join(
            b["text"] for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)
        )
        start = parse_time(self.message[1]) or when
        chat = self.chat(message_id, usage, start, when, response_id=message_id, output_text=text)
        for block in blocks:
            call_id, name = short(block.get("id")), short(block.get("name"))
            if block.get("type") == "tool_use" and call_id and name:
                self.tool_started(call_id, name, when, chat, block.get("input"))

    def own_span(self, first: datetime, last: datetime) -> Span:
        if not self.subagent:
            return super().own_span(first, last)
        return Span(
            trace_id=self.trace,
            span_id=self.parent,
            name="invoke_agent subagent",
            start_time=first,
            end_time=last,
            parent_span_id=self.root,
            agent_id=self.agent.agent_id,
            attributes={
                **self.session_attributes(),
                semconv.GEN_AI_AGENT_NAME: "subagent",
                semconv.GEN_AI_AGENT_ID: self.subagent,
            },
        )

    def extra_state(self) -> dict[str, Any]:
        return {"subagent": self.subagent, "prev": iso(self.prev), "message": self.message}
