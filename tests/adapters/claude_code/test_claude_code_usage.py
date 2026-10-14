import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openvigie.adapters.base import AdapterState
from openvigie.adapters.claude_code import ClaudeCodeAdapter
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, UsageQuality, UsageRecord

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".claude"
SESSION = "2fd0caa4-c2cd-5ae1-bd48-3f6035730ed4"


def setup(tmp_path: Path) -> tuple[ClaudeCodeAdapter, AgentInstance, Any, Path]:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home / ".claude")
    ctx = fake_context(homes=[home])
    adapter = ClaudeCodeAdapter()
    (agent,) = adapter.detect(ctx)
    return adapter, agent, ctx, home / ".claude"


def totals(records: list[UsageRecord]) -> dict[str, int]:
    keys = ["input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens"]  # fmt: skip
    return {k: sum(getattr(r, k) or 0 for r in records) for k in keys}


def test_recorded_fixture_totals(tmp_path: Path) -> None:
    adapter, agent, ctx, _ = setup(tmp_path)
    batch = adapter.collect(ctx, agent, AdapterState())
    assert len(batch.usage) == 9  # 9 API responses, many spread over several lines
    assert totals(batch.usage) == {
        "input_tokens": 18,
        "output_tokens": 24009,
        "cache_read_input_tokens": 529331,
        "cache_creation_input_tokens": 190288,
    }
    assert {r.response_model for r in batch.usage} == {"claude-opus-5-5"}
    # main thread writes with the 1-hour TTL, the subagent with 5 minutes; pricing needs the split
    assert sum(r.cache_creation_1h_input_tokens or 0 for r in batch.usage) == 54784
    assert all(r.quality is UsageQuality.EXACT and r.provider == "anthropic" for r in batch.usage)
    (session,) = batch.sessions
    assert session.session_id == SESSION and session.models == ("claude-opus-5-5",)
    assert session.started_at is not None and session.last_event_at is not None
    assert session.started_at <= session.last_event_at
    assert batch.last_activity_at == session.last_event_at


def test_rereading_yields_nothing_new(tmp_path: Path) -> None:
    adapter, agent, ctx, _ = setup(tmp_path)
    state = AdapterState()
    adapter.collect(ctx, agent, state)
    state.commit()
    assert adapter.collect(ctx, agent, state).usage == []


def assistant(msg_id: str, usage: dict[str, Any] | None, model: str = "claude-sonnet-4-5",
              ts: str = "2026-10-14T10:00:00Z", **extra: Any) -> dict[str, Any]:  # fmt: skip
    message: dict[str, Any] = {"id": msg_id, "model": model, "content": [{"type": "text"}]}
    if usage is not None:
        message["usage"] = usage
    return {"type": "assistant", "sessionId": "s-1", "requestId": "req-" + msg_id,
            "timestamp": ts, "message": message, **extra}  # fmt: skip


def write(path: Path, *entries: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for entry in entries:
            fh.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")


def test_edge_cases(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    state = AdapterState()
    adapter.collect(ctx, agent, state)
    state.commit()
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
             "output_tokens_details": {"thinking_tokens": 3}}  # fmt: skip
    write(
        root / "projects" / "-tmp-x" / "s-1.jsonl",
        assistant("m1", usage),
        assistant("m1", usage),  # same response, second content block
        assistant("m2", None),  # usage missing: unknown, not zero
        assistant("m3", usage, model="<synthetic>"),
        {"type": "user", "message": {"content": "secret prompt"}},
        "{not json",
        assistant("m4", {"input_tokens": -5, "output_tokens": "7"}),
    )
    batch = adapter.collect(ctx, agent, state)
    by_id = {r.record_id: r for r in batch.usage}
    assert len(by_id) == 3  # m1 once, m2, m4; synthetic skipped
    m1 = next(r for r in batch.usage if r.input_tokens == 10)
    assert m1.reasoning_tokens == 3 and m1.quality is UsageQuality.EXACT
    unknown = [r for r in batch.usage if r.quality is UsageQuality.UNKNOWN]
    assert len(unknown) == 1 and unknown[0].input_tokens is None
    nonsense = next(r for r in batch.usage if r not in unknown and r is not m1)
    assert nonsense.input_tokens is None and nonsense.output_tokens is None
    assert batch.stats.parse_errors == 1
    assert "secret prompt" not in repr(batch)


def test_appended_lines_are_read_incrementally(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    state = AdapterState()
    adapter.collect(ctx, agent, state)
    state.commit()
    path = root / "projects" / "-tmp-x" / "s-1.jsonl"
    write(path, assistant("a", {"input_tokens": 1, "output_tokens": 1}))
    first = adapter.collect(ctx, agent, state)
    state.commit()
    write(path, assistant("b", {"input_tokens": 2, "output_tokens": 2}))
    second = adapter.collect(ctx, agent, state)
    assert [r.input_tokens for r in first.usage] == [1]
    assert [r.input_tokens for r in second.usage] == [2]


def test_backfill_window_skips_old_history(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    write(root / "projects" / "-tmp-x" / "s-1.jsonl",
          assistant("old", {"input_tokens": 1}, ts="2026-01-01T00:00:00Z"),
          assistant("new", {"input_tokens": 2}, ts="2026-10-14T00:00:00Z"))  # fmt: skip
    state = AdapterState(backfill_since=datetime(2026, 10, 1, tzinfo=UTC))
    batch = adapter.collect(ctx, agent, state)
    assert [r.input_tokens for r in batch.usage if r.session_id == "s-1"] == [2]


def test_unknown_agent_collects_nothing(tmp_path: Path) -> None:
    adapter, _, ctx, _ = setup(tmp_path)
    stranger = AgentInstance("other", "h", "claude-code", "x")
    assert adapter.collect(ctx, stranger, AdapterState()).usage == []
