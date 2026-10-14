import dataclasses
import json
import os
import shutil
from pathlib import Path
from typing import Any

from openvigie import semconv
from openvigie.adapters.base import AdapterState, TraceOptions
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.spans import CONTENT_MAX_CHARS, span_id, trace_id
from openvigie.adapters.testing import fake_context
from openvigie.model import Span, SpanStatus, to_dict

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".claude"
GOLDEN = Path(__file__).parent / "fixtures" / "spans.golden.json"
SESSION = "2fd0caa4-c2cd-5ae1-bd48-3f6035730ed4"
AGENT_ID = "agent-claude-fixture"
TRANSCRIPT = f"projects/-home-user-openvigie/{SESSION}.jsonl"


def collect(home: Path, state: AdapterState, **options: bool) -> list[Span]:
    ctx = fake_context(homes=[home])
    ctx.traces = TraceOptions(**options)
    adapter = ClaudeCodeAdapter()
    (agent,) = adapter.detect(ctx)
    # agent ids derive from the home path; pin one so spans compare across temp dirs
    adapter.roots = {AGENT_ID: adapter.roots[agent.agent_id]}
    spans = adapter.collect(ctx, dataclasses.replace(agent, agent_id=AGENT_ID), state).spans
    state.commit()
    return spans


def fixture_home(tmp_path: Path) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "home" / ".claude")
    return tmp_path / "home"


def as_json(spans: list[Span]) -> list[dict[str, Any]]:
    return [to_dict(s) for s in sorted(spans, key=lambda s: (s.start_time, s.span_id))]


def test_golden_span_tree(tmp_path: Path) -> None:
    spans = collect(fixture_home(tmp_path), AdapterState(), enabled=True)
    actual = as_json(spans)
    if os.environ.get("OPENVIGIE_UPDATE_GOLDEN"):
        GOLDEN.write_text(json.dumps(actual, indent=1, sort_keys=True) + "\n")
    assert actual == json.loads(GOLDEN.read_text())

    # the shape, spelled out: one trace, one root, 9 responses, every parent present
    by_id = {s.span_id: s for s in spans}
    assert len({s.trace_id for s in spans}) == 1
    (root,) = [s for s in spans if s.parent_span_id is None]
    assert root.name == "invoke_agent claude-code"
    assert root.attributes[semconv.SESSION_ID] == SESSION
    assert root.span_id == span_id(root.trace_id, "root")
    chats = [s for s in spans if s.name.startswith("chat ")]
    assert len(chats) == 9
    assert sum(int(c.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS]) for c in chats) == 24009
    assert all(s.parent_span_id in by_id for s in spans if s is not root)
    (subagent,) = [s for s in spans if s.name == "invoke_agent subagent"]
    assert subagent.parent_span_id == root.span_id
    tools = [s for s in spans if s.name.startswith("execute_tool ")]
    assert all(by_id[t.parent_span_id or ""].name.startswith("chat ") for t in tools)
    # the recording stops before the last tool returned: that span stays open
    assert [t.status for t in tools if t.end_time is None] == [SpanStatus.UNSET]
    assert all(s.end_time is None or s.start_time <= s.end_time for s in spans)


def test_ids_are_deterministic(tmp_path: Path) -> None:
    first = collect(fixture_home(tmp_path / "a"), AdapterState(), enabled=True)
    second = collect(fixture_home(tmp_path / "b"), AdapterState(), enabled=True)
    assert as_json(first) == as_json(second)
    assert {s.trace_id for s in first} == {trace_id(AGENT_ID, SESSION)}


def test_incremental_read_matches_one_pass(tmp_path: Path) -> None:
    whole = {s.span_id: s for s in collect(fixture_home(tmp_path / "a"), AdapterState(),
                                           enabled=True)}  # fmt: skip
    home = fixture_home(tmp_path / "b")
    path = home / ".claude" / TRANSCRIPT
    lines = path.read_text().splitlines(keepends=True)
    state = AdapterState()
    seen: dict[str, Span] = {}
    for cut in (24, 35, 45, len(lines)):  # mid-response and mid-tool boundaries
        path.write_text("".join(lines[:cut]))
        seen.update({s.span_id: s for s in collect(home, state, enabled=True)})
    assert seen == whole


def test_no_spans_by_default_and_no_content_unless_asked(tmp_path: Path) -> None:
    home = fixture_home(tmp_path)
    assert collect(home, AdapterState()) == []
    spans = collect(home, AdapterState(), enabled=True)
    assert spans and not any(semconv.CONTENT_ATTRIBUTES & set(s.attributes) for s in spans)


def test_content_is_redacted_and_truncated(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "a" * 90
    project = tmp_path / "home" / ".claude" / "projects" / "-p"
    project.mkdir(parents=True)
    entries = [
        {"type": "user", "sessionId": "s1", "timestamp": "2026-10-14T10:00:00Z",
         "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "sessionId": "s1", "timestamp": "2026-10-14T10:00:02Z",
         "requestId": "r1", "message": {
             "id": "m1", "model": "claude-opus-5-5", "usage": {"output_tokens": 5},
             "content": [{"type": "text", "text": f"key is {secret}"},
                         {"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {"command": f"export ANTHROPIC_API_KEY={secret}"}}]}},
        {"type": "user", "sessionId": "s1", "timestamp": "2026-10-14T10:00:03Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "is_error": True,
              "content": "x" * (CONTENT_MAX_CHARS * 2)}]}},
    ]  # fmt: skip
    (project / "s1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    spans = collect(tmp_path / "home", AdapterState(), enabled=True, export_content=True)
    exported = json.dumps([to_dict(s) for s in spans])
    assert secret not in exported and "[REDACTED" in exported
    (tool,) = [s for s in spans if s.name == "execute_tool Bash"]
    assert tool.status is SpanStatus.ERROR and tool.attributes[semconv.ERROR_TYPE] == "tool_error"
    result = str(tool.attributes[semconv.GEN_AI_TOOL_CALL_RESULT])
    assert len(result) <= CONTENT_MAX_CHARS and result.endswith("[truncated]")
    assert semconv.GEN_AI_TOOL_CALL_ARGUMENTS in tool.attributes
    (chat,) = [s for s in spans if s.name.startswith("chat ")]
    assert "key is" in str(chat.attributes[semconv.GEN_AI_OUTPUT_MESSAGES])
    assert (chat.end_time - chat.start_time).total_seconds() == 2  # type: ignore[operator]
