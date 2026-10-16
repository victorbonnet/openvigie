import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import AdapterState
from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes import usage as hermes_usage
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, UsageQuality
from openvigie.safeio import open_sqlite_readonly

FIXTURE = Path(__file__).parent / "fixtures" / "0.21.7" / "home" / ".hermes"
ROWS = 196
OUTPUT_TOKENS = 2_639_349
INPUT_TOKENS = 465_513_459  # OpenAI rows count their cache reads inside input
CODEX_SESSION = "20260912_101456_b71ea675"


def setup(tmp_path: Path) -> tuple[HermesAdapter, AgentInstance, Path]:
    root = tmp_path / "home" / ".hermes"
    shutil.copytree(FIXTURE, root)
    adapter = HermesAdapter()
    (agent,) = adapter.detect(fake_context(homes=[tmp_path / "home"]))
    return adapter, agent, root / "state.db"


def collect(adapter: HermesAdapter, agent: AgentInstance, state: AdapterState):  # type: ignore[no-untyped-def]
    batch = adapter.collect(fake_context(homes=[]), agent, state)
    state.commit()
    return batch


def grow(db: Path, session: str, tokens: int, last_seen: float) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "update session_model_usage set output_tokens = output_tokens + ?, last_seen = ? "
            "where session_id = ? and model = 'gpt-5.6-sol' and task = ''",
            (tokens, last_seen, session),
        )


def test_reads_every_row_once(tmp_path: Path) -> None:
    adapter, agent, _ = setup(tmp_path)
    state = AdapterState()
    batch = collect(adapter, agent, state)
    assert len(batch.usage) == ROWS
    assert sum(r.output_tokens or 0 for r in batch.usage) == OUTPUT_TOKENS
    assert sum(r.input_tokens or 0 for r in batch.usage) == INPUT_TOKENS
    assert all(r.quality is UsageQuality.EXACT for r in batch.usage)
    assert batch.backlog is not None and batch.backlog.complete
    assert batch.sessions and batch.last_activity_at is not None
    assert collect(adapter, agent, state).usage == []


def test_codex_rows_are_priced_as_openai(tmp_path: Path) -> None:
    adapter, agent, _ = setup(tmp_path)
    batch = collect(adapter, agent, AdapterState())
    (record,) = [r for r in batch.usage if r.session_id == CODEX_SESSION and r.input_tokens
                 and r.input_tokens > 9_000_000]  # fmt: skip
    assert record.provider == "openai"
    assert record.request_model == "gpt-5.6-sol"
    assert record.input_tokens == 821_287 + 8_748_928
    assert record.cache_read_input_tokens == 8_748_928


def test_routed_models_have_no_provider(tmp_path: Path) -> None:
    adapter, agent, _ = setup(tmp_path)
    batch = collect(adapter, agent, AdapterState())
    routed = [r for r in batch.usage if r.request_model and "/" in r.request_model]
    assert routed and all(r.provider is None for r in routed)


def test_a_growing_session_reports_only_the_growth(tmp_path: Path) -> None:
    adapter, agent, db = setup(tmp_path)
    state = AdapterState()
    collect(adapter, agent, state)
    grow(db, CODEX_SESSION, 500, 1_792_000_000.0)
    (record,) = collect(adapter, agent, state).usage
    assert record.session_id == CODEX_SESSION
    assert record.output_tokens == 500
    assert record.input_tokens == 0
    assert record.timestamp == datetime.fromtimestamp(1_792_000_000.0, UTC)


def test_uncommitted_reads_are_read_again(tmp_path: Path) -> None:
    adapter, agent, _ = setup(tmp_path)
    state = AdapterState()
    first = adapter.collect(fake_context(homes=[]), agent, state)
    state.rollback()
    again = adapter.collect(fake_context(homes=[]), agent, state)
    assert [r.record_id for r in again.usage] == [r.record_id for r in first.usage]


def test_backfill_window(tmp_path: Path) -> None:
    adapter, agent, _ = setup(tmp_path)
    since = datetime.fromtimestamp(1_790_000_000, UTC)
    batch = collect(adapter, agent, AdapterState(backfill_since=since))
    assert batch.usage
    assert min(r.timestamp for r in batch.usage) >= since
    assert len(batch.usage) < ROWS


def test_forgotten_session_resumed_only_sets_a_baseline(tmp_path: Path) -> None:
    adapter, agent, db = setup(tmp_path)
    state = AdapterState()
    collect(adapter, agent, state)
    saved = state.get(f"usage:{agent.agent_id}")
    assert saved is not None
    state.committed[f"usage:{agent.agent_id}"] = {"seen": saved["seen"], "rows": {}}
    grow(db, CODEX_SESSION, 500, 1_792_000_000.0)
    assert collect(adapter, agent, state).usage == []
    grow(db, CODEX_SESSION, 7, 1_792_000_100.0)
    (record,) = collect(adapter, agent, state).usage
    assert record.output_tokens == 7


def test_profile_usage_goes_to_the_profile(tmp_path: Path) -> None:
    adapter, _, db = setup(tmp_path)
    profile = tmp_path / "home" / ".hermes" / "profiles" / "profile-1"
    profile.mkdir(parents=True)
    shutil.copy(db, profile / "state.db")
    with sqlite3.connect(profile / "state.db") as conn:
        conn.execute("delete from session_model_usage where session_id != ?", (CODEX_SESSION,))
    agents = {a.name: a for a in adapter.detect(fake_context(homes=[tmp_path / "home"]))}
    agent = agents["Hermes · profile-1"]
    usage = collect(adapter, agent, AdapterState()).usage
    assert len(usage) == 5
    assert {r.agent_id for r in usage} == {agent.agent_id}
    assert {r.session_id for r in usage} == {CODEX_SESSION}


def test_no_state_db(tmp_path: Path) -> None:
    adapter, agent, db = setup(tmp_path)
    db.unlink()
    batch = collect(adapter, agent, AdapterState())
    assert batch.usage == []
    assert batch.backlog is not None and batch.backlog.complete


def test_unreadable_state_db_degrades(tmp_path: Path) -> None:
    adapter, agent, db = setup(tmp_path)
    db.write_bytes(b"not a database" * 100)
    assert collect(adapter, agent, AdapterState()).usage == []
    assert adapter.health().reason == "state.db has no readable usage table"


def test_missing_counts_are_unknown_not_zero(tmp_path: Path) -> None:
    adapter, agent, db = setup(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "update session_model_usage set input_tokens = 'n/a' where session_id = ?",
            (CODEX_SESSION,),
        )
    batch = collect(adapter, agent, AdapterState())
    mine = [r for r in batch.usage if r.session_id == CODEX_SESSION]
    assert mine and all(r.quality is UsageQuality.UNKNOWN for r in mine)
    assert all(r.input_tokens is None for r in mine)


def test_state_db_is_opened_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter, agent, _ = setup(tmp_path)
    writes: list[str] = []

    def opener(path: Path) -> sqlite3.Connection:
        conn = open_sqlite_readonly(path)
        try:
            conn.execute("update session_model_usage set output_tokens = 0")
        except sqlite3.OperationalError as e:
            writes.append(str(e))
        return conn

    monkeypatch.setattr(hermes_usage, "open_sqlite_readonly", opener)
    assert collect(adapter, agent, AdapterState()).usage
    assert writes and "readonly" in writes[0]
