import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import ActivityBatch, AdapterState
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.openclaw import OpenClawAdapter
from openvigie.adapters.openclaw import usage as openclaw_usage
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, UsageQuality

FIXTURE = Path(__file__).parent / "fixtures" / "2026.9.4" / "home" / ".openclaw"
CODEX = Path(__file__).parents[1] / "codex" / "fixtures" / "home" / ".codex"
SESSION = "1c1ccc7d-27f5-4c88-9b5c-4095e6f80706"
OTHER = "352a19c5-6a6e-40aa-b8aa-f08214936b91"
ZERO = "16ee7327-d91e-4c39-82c3-5cd05941dba4"
TRANSCRIPT = FIXTURE / "agents" / "main" / "sessions" / f"{SESSION}.jsonl"


def setup(tmp_path: Path) -> tuple[OpenClawAdapter, dict[str, AgentInstance], Path]:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home / ".openclaw")
    adapter = OpenClawAdapter()
    agents = {a.name.split(" · ")[1]: a for a in adapter.detect(fake_context(homes=[home]))}
    return adapter, agents, home / ".openclaw" / "agents"


def collect(adapter: OpenClawAdapter, agent: AgentInstance, state: AdapterState) -> ActivityBatch:
    batch = adapter.collect(fake_context(homes=[]), agent, state)
    state.commit()
    return batch


def move_to_database(agent_dir: Path, lines: list[str]) -> None:
    """What OpenClaw 2026.9 does with a transcript: one row per line in transcript_events."""
    with sqlite3.connect(agent_dir / "agent" / "openclaw-agent.sqlite") as conn:
        conn.execute("delete from transcript_events")
        conn.executemany(
            "insert into transcript_events (session_id, seq, event_json, created_at) "
            "values (?, ?, ?, 0)",
            [(SESSION, seq, line) for seq, line in enumerate(lines)],
        )


def test_reads_transcripts_but_not_trajectories(tmp_path: Path) -> None:
    adapter, agents, _ = setup(tmp_path)
    state = AdapterState()
    batch = collect(adapter, agents["main"], state)
    totals = {
        session: tuple(
            sum(getattr(r, f) or 0 for r in batch.usage if r.session_id == session)
            for f in ("input_tokens", "output_tokens", "cache_read_input_tokens")
        )
        for session in (SESSION, OTHER)
    }
    assert totals == {SESSION: (26_560, 1_435, 38_400), OTHER: (18_635, 929, 28_928)}
    routed = [r for r in batch.usage if r.session_id in (SESSION, OTHER)]
    assert len(routed) == 6
    assert all(r.quality is UsageQuality.EXACT and r.provider is None for r in routed)
    # all-zero usage whose stopReason the recorder scrubbed: unknown, not zero
    (zero,) = [r for r in batch.usage if r.session_id == ZERO]
    assert zero.quality is UsageQuality.UNKNOWN and zero.input_tokens is None
    assert {s.models for s in batch.sessions if s.session_id != ZERO} == {
        ("moonshotai/Kimi-K2.6:deepinfra",)}  # fmt: skip
    assert batch.backlog is not None and batch.backlog.complete
    assert collect(adapter, agents["main"], state).usage == []


def test_database_events_count_once_with_the_transcript(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    from_file = collect(adapter, agents["main"], AdapterState()).usage
    move_to_database(root / "main", TRANSCRIPT.read_text().splitlines())
    both = collect(adapter, agents["main"], AdapterState()).usage
    assert {r.record_id for r in both} == {r.record_id for r in from_file}
    (root / "main" / "sessions" / TRANSCRIPT.name).unlink()
    state = AdapterState()
    from_db = collect(adapter, agents["main"], state).usage
    assert {r.record_id for r in from_db} == {r.record_id for r in from_file}
    assert collect(adapter, agents["main"], state).usage == []


def test_openai_input_counts_cache_reads(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    event = {
        "type": "message",
        "id": "a1b2c3d4",
        "timestamp": "2026-10-14T09:00:00.000Z",
        "message": {"role": "assistant", "provider": "openai", "model": "gpt-5.5",
                    "usage": {"input": 100, "output": 20, "cacheRead": 900, "cacheWrite": 0},
                    "content": [{"type": "text", "text": "never kept"}]},
    }  # fmt: skip
    move_to_database(root / "vulpes", [json.dumps(event)])
    (record,) = collect(adapter, agents["vulpes"], AdapterState()).usage
    assert record.provider == "openai"
    assert record.input_tokens == 1_000
    assert record.cache_read_input_tokens == 900
    assert record.timestamp == datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    assert "never kept" not in repr(record)


def test_codex_harness_sessions(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    shutil.copytree(CODEX / "sessions", root / "lutra" / "agent" / "codex-home" / "sessions")
    codex = CodexAdapter()
    (codex_agent,) = codex.detect(fake_context(homes=[CODEX.parent]))
    expected = codex.collect(fake_context(homes=[]), codex_agent, AdapterState()).usage
    state = AdapterState()
    batch = collect(adapter, agents["lutra"], state)
    assert batch.usage
    assert sum(r.input_tokens or 0 for r in batch.usage) == sum(
        r.input_tokens or 0 for r in expected
    )
    assert {r.provider for r in batch.usage} == {"openai"}
    assert {r.agent_id for r in batch.usage} == {agents["lutra"].agent_id}
    assert collect(adapter, agents["lutra"], state).usage == []


def test_backfill_window(tmp_path: Path) -> None:
    adapter, agents, _ = setup(tmp_path)
    late = AdapterState(backfill_since=datetime(2026, 6, 19, 6, 0, 30, tzinfo=UTC))
    assert len(collect(adapter, agents["main"], late).usage) == 3  # two routed, one unknown


def test_agent_without_sessions(tmp_path: Path) -> None:
    adapter, agents, _ = setup(tmp_path)
    batch = collect(adapter, agents["lutra"], AdapterState())
    assert batch.usage == []
    assert batch.backlog is not None and batch.backlog.complete


def test_unreadable_database_degrades(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    (root / "vulpes" / "agent" / "openclaw-agent.sqlite").write_bytes(b"not a database" * 100)
    batch = collect(adapter, agents["vulpes"], AdapterState())
    assert batch.usage == []
    assert batch.backlog is None
    assert "transcript_events" in (adapter.health().reason or "")


def test_missing_input_is_unknown_and_content_never_kept(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    event = {
        "type": "message",
        "id": "e5f6a7b8",
        "timestamp": "2026-10-14T10:00:00.000Z",
        "message": {"role": "assistant", "provider": "anthropic", "model": "claude-sonnet-4-6",
                    "usage": {"output": 40},
                    "content": [{"type": "text", "text": "hello from alice"}]},
    }  # fmt: skip
    move_to_database(root / "vulpes", [json.dumps(event)])
    batch = collect(adapter, agents["vulpes"], AdapterState())
    (record,) = batch.usage
    assert record.input_tokens is None
    assert record.quality is UsageQuality.UNKNOWN
    assert "alice" not in repr(batch.usage) + repr(batch.sessions)


def event(n: int, **message: object) -> str:
    body = {
        "role": "assistant",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "usage": {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0},
        **message,
    }
    return json.dumps({"type": "message", "id": f"ev{n:04}",
                       "timestamp": "2026-10-14T10:00:00.000Z", "message": body})  # fmt: skip


def insert(agent_dir: Path, session: str, lines: list[str]) -> None:
    with sqlite3.connect(agent_dir / "agent" / "openclaw-agent.sqlite") as conn:
        conn.executemany(
            "insert into transcript_events (session_id, seq, event_json, created_at) "
            "values (?, ?, ?, 0)",
            [(session, seq, line) for seq, line in enumerate(lines)],
        )


def test_rowids_reused_after_the_newest_rows_are_deleted(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    move_to_database(root / "vulpes", [])
    insert(root / "vulpes", "a", [event(i) for i in range(3)])
    insert(root / "vulpes", "b", [event(i) for i in range(3, 8)])
    state = AdapterState()
    assert len(collect(adapter, agents["vulpes"], state).usage) == 8
    with sqlite3.connect(root / "vulpes" / "agent" / "openclaw-agent.sqlite") as conn:
        conn.execute("delete from transcript_events where session_id = 'b'")
    insert(root / "vulpes", "c", [event(i) for i in range(8, 12)])  # rowids 4-7 again
    again = collect(adapter, agents["vulpes"], state).usage
    assert {r.session_id for r in again if r.session_id == "c"} == {"c"}
    assert len([r for r in again if r.session_id == "c"]) == 4


def test_full_pages_resume_on_the_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openclaw_usage, "ROWS_PER_TICK", 3)
    adapter, agents, root = setup(tmp_path)
    move_to_database(root / "vulpes", [event(i) for i in range(5)])
    state = AdapterState()
    first = collect(adapter, agents["vulpes"], state)
    assert len(first.usage) == 3 and first.backlog is not None and first.backlog.truncated
    second = collect(adapter, agents["vulpes"], state)
    assert len(second.usage) == 2 and second.backlog is not None and second.backlog.complete


def test_invalid_and_large_rows(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    big = [{"type": "text", "text": "x" * (2 * 1024 * 1024)}]
    move_to_database(root / "vulpes", ["{not json", event(1, content=big)])
    batch = collect(adapter, agents["vulpes"], AdapterState())
    assert batch.stats.parse_errors == 1
    (record,) = batch.usage  # a row over 1 MiB still counts
    assert (record.input_tokens, record.output_tokens) == (10, 5)


def test_backfill_uses_the_message_time_when_the_event_has_none(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    old = json.loads(event(1, timestamp=1735689600000))  # 2025-01-01, in ms
    del old["timestamp"]
    move_to_database(root / "vulpes", [json.dumps(old)])
    since = AdapterState(backfill_since=datetime(2026, 10, 1, tzinfo=UTC))
    assert collect(adapter, agents["vulpes"], since).usage == []
    (record,) = collect(adapter, agents["vulpes"], AdapterState()).usage
    assert record.timestamp == datetime(2025, 1, 1, tzinfo=UTC)


def test_zero_usage_is_skipped_only_for_failed_calls(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    zero = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    move_to_database(root / "vulpes", [event(1, usage=zero, stopReason="error"),
                                       event(2, usage=zero, stopReason="aborted"),
                                       event(3, usage=zero, stopReason="stop")])  # fmt: skip
    (record,) = collect(adapter, agents["vulpes"], AdapterState()).usage
    assert record.quality is UsageQuality.UNKNOWN
    assert (record.input_tokens, record.output_tokens) == (None, None)


def test_openai_without_cache_reads_is_estimated(tmp_path: Path) -> None:
    adapter, agents, root = setup(tmp_path)
    move_to_database(root / "vulpes", [event(1, provider="openai", model="gpt-5.5",
                                             usage={"input": 100, "output": 20})])  # fmt: skip
    (record,) = collect(adapter, agents["vulpes"], AdapterState()).usage
    assert record.quality is UsageQuality.ESTIMATED and record.input_tokens == 100
