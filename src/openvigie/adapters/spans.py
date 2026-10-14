"""Shared helpers for building OTel GenAI spans from agent transcripts (FR-6).

Ids are deterministic, so a re-read yields identical spans and the hub can build trace links
without asking the backend: ``trace_id = stable_id(agent_id, session_id)`` (16 bytes) and
``span_id = stable_id(trace_id, key)[:16]`` (8 bytes).

Content (prompts, outputs, tool arguments and results) is left out unless
``TraceOptions.export_content`` is on; then it is redacted first and truncated.
"""

from __future__ import annotations

import json
from typing import Any

from openvigie.model import stable_id
from openvigie.redact import redact, redact_obj

CONTENT_MAX_CHARS = 4096
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
        text = text[:CONTENT_MAX_CHARS] + TRUNCATED
    return text
