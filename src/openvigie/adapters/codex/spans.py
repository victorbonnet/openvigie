"""Codex rollouts → OTel GenAI spans (FR-6).

One trace per rollout (session):

- ``invoke_agent codex``: root span, from the first to the last rollout entry;
- ``chat <model>``: one per ``token_count`` that adds usage, from the latest input (user message
  or tool output) to that event;
- ``execute_tool <name>``: one per ``function_call`` / ``custom_tool_call`` and its output, with
  the exit status when the output reports one. Shell command lines and outputs can hold secrets:
  they are only exported with ``export_content``, redacted.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openvigie.adapters.spans import CONTENT_MAX_CHARS, SessionSpans, iso, short, span_id
from openvigie.adapters.usage import parse_time
from openvigie.model import SpanStatus, UsageRecord

CALLS = {"function_call", "custom_tool_call"}
OUTPUTS = {"function_call_output", "custom_tool_call_output"}
EXIT_LINE = re.compile(r"\AExit code: (-?\d{1,5})\b")
MAX_OUTPUT_PARSE = 1024 * 1024


def exit_code(output: object) -> int | None:
    """The exit status a tool output reports: JSON ``metadata.exit_code`` or "Exit code: N"."""
    if isinstance(output, dict):  # newer rollouts: {"content": "...", "success": bool}
        output = output.get("content")
    if not isinstance(output, str):
        return None
    match = EXIT_LINE.match(output)
    if match:
        return int(match.group(1))
    if not output.startswith("{") or len(output) > MAX_OUTPUT_PARSE:
        return None
    try:
        parsed = json.loads(output)
    except ValueError:
        return None
    metadata = parsed.get("metadata") if isinstance(parsed, dict) else None
    code = metadata.get("exit_code") if isinstance(metadata, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


class RolloutSpans(SessionSpans):
    """Spans of one Codex rollout file."""

    agent_name = "codex"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        count = self.saved.get("chats")
        self.chats = count if isinstance(count, int) and count >= 0 else 0
        self.input_at = parse_time(self.saved.get("input_at"))  # latest input to the model
        self.text = ""  # assistant text waiting for its token_count (content export only)

    @property
    def next_chat(self) -> str:
        """The chat span the next ``token_count`` will close; tool calls come before it."""
        return span_id(self.trace, f"chat:{self.chats}")

    def add(self, entry: object, session: str | None, usage: UsageRecord | None) -> None:
        if not isinstance(entry, dict):
            return
        when = parse_time(entry.get("timestamp"))
        self.session = self.session or short(session)
        if when is None or self.session is None:
            return
        self.touch(when)
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if usage is not None:
            start = self.input_at or self.first or when
            self.chat(str(self.chats), usage, start, when, output_text=self.text)
            self.chats += 1
            self.input_at, self.text = when, ""
        elif entry.get("type") != "response_item":
            return
        elif kind == "message":
            if payload.get("role") == "user":
                self.input_at = when
            elif self.export_content and payload.get("role") == "assistant":
                self.text = (self.text + _text(payload.get("content")))[: CONTENT_MAX_CHARS * 2]
        elif kind in CALLS:
            call_id, name = short(payload.get("call_id")), short(payload.get("name"))
            if call_id and name:
                arguments = payload.get("arguments", payload.get("input"))
                self.tool_started(call_id, name, when, self.next_chat, arguments)
        elif kind in OUTPUTS:
            call_id = short(payload.get("call_id"))
            if call_id:
                self.input_at = when
                output = payload.get("output")
                code = exit_code(output)
                self.tool_finished(
                    call_id,
                    when,
                    error="nonzero_exit" if code else None,
                    status=None if code is not None else SpanStatus.UNSET,
                    result=output,
                )

    def extra_state(self) -> dict[str, Any]:
        return {"chats": self.chats, "input_at": iso(self.input_at)}


def _text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and part.get("type") == "output_text"
        and isinstance(part.get("text"), str)
    )  # fmt: skip
