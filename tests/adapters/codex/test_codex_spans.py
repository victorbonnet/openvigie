import dataclasses
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from openvigie import semconv
from openvigie.adapters.base import AdapterState, TraceOptions
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.codex.spans import exit_code
from openvigie.adapters.spans import trace_id
from openvigie.adapters.testing import fake_context
from openvigie.model import Span, SpanStatus, to_dict

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".codex"
GOLDEN = Path(__file__).parent / "fixtures" / "spans.golden.json"
S1 = "0199e5a0-1111-7000-8000-000000000001"
AGENT_ID = "agent-codex-fixture"
ROLLOUT = (
    "sessions/2026/10/14/rollout-2026-10-14T09-00-00-0199e5a0-1111-7000-8000-000000000001.jsonl"
)


def collect(home: Path, state: AdapterState, **options: bool) -> list[Span]:
    ctx = fake_context(homes=[home])
    ctx.traces = TraceOptions(**options)
    adapter = CodexAdapter()
    (agent,) = adapter.detect(ctx)
    # agent ids derive from the home path; pin one so spans compare across temp dirs
    adapter.roots = {AGENT_ID: adapter.roots[agent.agent_id]}
    adapter.providers = {AGENT_ID: adapter.providers[agent.agent_id]}
    spans = adapter.collect(ctx, dataclasses.replace(agent, agent_id=AGENT_ID), state).spans
    state.commit()
    return spans


def fixture_home(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "home" / ".codex")
    return tmp_path / "home"


def as_json(spans: list[Span]) -> list[dict[str, Any]]:
    return [to_dict(s) for s in sorted(spans, key=lambda s: (s.start_time, s.span_id))]


def test_golden_span_tree(tmp_path: Path) -> None:
    spans = collect(fixture_home(tmp_path), AdapterState(), enabled=True)
    actual = as_json(spans)
    if os.environ.get("OPENVIGIE_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert actual == json.loads(GOLDEN.read_text())

    session = [s for s in spans if s.trace_id == trace_id(AGENT_ID, S1)]
    by_name = {s.name: s for s in session}
    root = by_name["invoke_agent codex"]
    assert root.parent_span_id is None and root.attributes[semconv.SESSION_ID] == S1
    chats = sorted((s for s in session if s.name == "chat gpt-5-codex"), key=lambda s: s.start_time)
    # two token_count deltas (the duplicate and the info: null event add nothing)
    assert [c.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] for c in chats] == [200, 450]
    assert chats[0].start_time.second == 2  # from the user message …
    assert chats[1].start_time.second == 9  # … and from the tool output
    tool = by_name["execute_tool shell"]
    assert tool.parent_span_id == chats[0].span_id
    assert tool.status is SpanStatus.OK and tool.end_time is not None
    assert len({s.trace_id for s in spans}) == 3  # one trace per rollout


def test_no_content_by_default(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    assert collect(home, AdapterState()) == []
    spans = collect(home, AdapterState(), enabled=True)
    assert spans and not any(semconv.CONTENT_ATTRIBUTES & set(s.attributes) for s in spans)


def test_incremental_read_matches_one_pass(tmp_path: Path) -> None:
    whole = {s.span_id: s for s in collect(fixture_home(tmp_path / "a"), AdapterState(),
                                           enabled=True)}  # fmt: skip
    home = fixture_home(tmp_path / "b")
    path = home / ".codex" / ROLLOUT
    lines = path.read_text().splitlines(keepends=True)
    state = AdapterState()
    seen: dict[str, Span] = {}
    for cut in (7, 9, len(lines)):  # after the call, after the first usage, the rest
        path.write_text("".join(lines[:cut]))
        seen.update({s.span_id: s for s in collect(home, state, enabled=True)})
    assert seen == whole


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ('{"output": "x", "metadata": {"exit_code": 2, "duration_seconds": 0.1}}', 2),
        ("Exit code: 0\nWall time: 0.2 seconds\nOutput:\nok", 0),
        ({"content": "Exit code: 1\n…", "success": False}, 1),
        ("plain text", None),
        ('{"metadata": {"exit_code": true}}', None),
        ("{not json", None),
    ],
)
def test_exit_code(output: object, code: int | None) -> None:
    assert exit_code(output) == code


def test_failed_command_and_content_opt_in(tmp_path: Path) -> None:
    secret = "sk-proj-" + "b" * 60
    day = tmp_path / "home" / ".codex" / "sessions" / "2026" / "10" / "14"
    day.mkdir(parents=True)
    entries = [
        ("session_meta", {"id": "s9", "cli_version": "0.46.0"}),
        ("turn_context", {"model": "gpt-5"}),
        ("response_item", {"type": "message", "role": "user", "content": []}),
        ("response_item", {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": "running it"}]}),
        ("response_item", {"type": "function_call", "name": "shell", "call_id": "c1",
                           "arguments": json.dumps({"command": ["curl", "-H", secret]})}),
        ("event_msg", {"type": "token_count", "info": {"total_token_usage": {
            "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5,
            "reasoning_output_tokens": 0}}}),
        ("response_item", {"type": "function_call_output", "call_id": "c1",
                           "output": f"Exit code: 7\nOutput:\n{secret}"}),
    ]  # fmt: skip
    lines = [
        json.dumps({"timestamp": f"2026-10-14T10:00:0{i}Z", "type": kind, "payload": payload})
        for i, (kind, payload) in enumerate(entries)
    ]
    (day / "rollout-2026-10-14T10-00-00-s9.jsonl").write_text("\n".join(lines) + "\n")

    (tool,) = [s for s in collect(tmp_path / "home", AdapterState(), enabled=True)
               if s.name == "execute_tool shell"]  # fmt: skip
    assert tool.status is SpanStatus.ERROR
    assert tool.attributes[semconv.ERROR_TYPE] == "nonzero_exit"
    assert not semconv.CONTENT_ATTRIBUTES & set(tool.attributes)  # no command line by default

    spans = collect(tmp_path / "home", AdapterState(), enabled=True, export_content=True)
    exported = json.dumps([to_dict(s) for s in spans])
    assert secret not in exported and "[REDACTED" in exported
    (chat,) = [s for s in spans if s.name == "chat gpt-5"]
    assert chat.attributes[semconv.GEN_AI_OUTPUT_MESSAGES] == "running it"
