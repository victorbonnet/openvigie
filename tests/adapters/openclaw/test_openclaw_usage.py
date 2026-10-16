import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from openvigie.adapters.base import ActivityBatch, AdapterState
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.openclaw import OpenClawAdapter
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, UsageQuality

FIXTURE = Path(__file__).parent / "fixtures" / "2026.9.4" / "home" / ".openclaw"
CODEX = Path(__file__).parents[1] / "codex" / "fixtures" / "home" / ".codex"
SESSION = "1c1ccc7d-27f5-4c88-9b5c-4095e6f80706"
OTHER = "352a19c5-6a6e-40aa-b8aa-f08214936b91"
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
        for session in {r.session_id for r in batch.usage}
    }
    assert totals == {SESSION: (26_560, 1_435, 38_400), OTHER: (18_635, 929, 28_928)}
    assert len(batch.usage) == 6  # the failed call with zero usage is skipped
    assert all(r.quality is UsageQuality.EXACT for r in batch.usage)
    assert all(r.provider is None for r in batch.usage)  # routed model: left unpriced
    assert {s.models for s in batch.sessions} == {("moonshotai/Kimi-K2.6:deepinfra",)}
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
    assert len(collect(adapter, agents["main"], late).usage) == 2


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
