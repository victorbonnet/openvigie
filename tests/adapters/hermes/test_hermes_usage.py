import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openvigie.adapters.base import AdapterState
from openvigie.adapters.hermes import HermesAdapter
from openvigie.adapters.hermes import usage as hermes_usage
from openvigie.adapters.testing import FIXED_NOW, fake_context
from openvigie.model import AgentInstance, UsageQuality
from openvigie.safeio import open_sqlite_readonly

FIXTURE = Path(__file__).parent / "fixtures" / "0.21.7" / "home" / ".hermes"
NO_PROFILES = shutil.ignore_patterns("profiles")  # recorded .env samples: test_hermes_access
ROWS = 196
OUTPUT_TOKENS = 2_639_349
INPUT_TOKENS = 465_513_459  # OpenAI rows count their cache reads inside input
CODEX_SESSION = "20260912_101456_b71ea675"


def setup(tmp_path: Path) -> tuple[HermesAdapter, AgentInstance, Path]:
    root = tmp_path / "home" / ".hermes"
    shutil.copytree(FIXTURE, root, ignore=NO_PROFILES)
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


def test_history_over_several_pages_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hermes_usage, "ROWS_PER_TICK", 50)
    adapter, agent, _ = setup(tmp_path)
    state = AdapterState()
    usage = []
    for _ in range(20):
        batch = collect(adapter, agent, state)
        usage += batch.usage
        if batch.backlog is not None and batch.backlog.complete:
            break
    assert len({r.record_id for r in usage}) == ROWS
    assert sum(r.output_tokens or 0 for r in usage) == OUTPUT_TOKENS


def test_usage_notes_name_the_profile(tmp_path: Path) -> None:
    adapter, _, _ = setup(tmp_path)
    profile = tmp_path / "home" / ".hermes" / "profiles" / "profile-1"
    profile.mkdir(parents=True)
    (profile / "state.db").write_bytes(b"not a database" * 100)
    agents = {a.name: a for a in adapter.detect(fake_context(homes=[tmp_path / "home"]))}
    collect(adapter, agents["Hermes · profile-1"], AdapterState())
    reason = adapter.health().reason or ""
    assert reason.count("profile profile-1: state.db has no readable usage table") == 1


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


def in_enclos(
    tmp_path: Path, *, wal: bool = False, **created: str
) -> tuple[HermesAdapter, dict[str, AgentInstance]]:
    """The fixture as enclos sandbox ``pi``'s home, with a profile ``alice``. ``created``:
    ``created_at`` in enclos's state.db for ``sandbox`` (required) and any profile; none means
    no state.db."""
    data = tmp_path / "home" / ".local/share/enclos"
    root = data / "sandboxes" / "pi" / "home" / ".hermes"
    shutil.copytree(FIXTURE, root, ignore=NO_PROFILES)
    (root / "profiles" / "alice").mkdir(parents=True)
    shutil.copy(FIXTURE / "state.db", root / "profiles" / "alice" / "state.db")
    if created:
        with sqlite3.connect(data / "state.db") as conn:
            if wal:
                conn.execute("pragma journal_mode=wal")
            conn.execute("create table sandboxes (name text, created_at text)")
            conn.execute("create table agents (sandbox text, name text, created_at text)")
            conn.execute("insert into sandboxes values ('pi', ?)", (created.pop("sandbox"),))
            conn.executemany("insert into agents values ('pi', ?, ?)", created.items())
    return detect_enclos(tmp_path, HermesAdapter())


def detect_enclos(
    tmp_path: Path, adapter: HermesAdapter
) -> tuple[HermesAdapter, dict[str, AgentInstance]]:
    agents = {a.name: a for a in adapter.detect(fake_context(homes=[tmp_path / "home"]))}
    return adapter, agents


ENCLOS_DB = "home/.local/share/enclos/state.db"
SANDBOX_DB = "home/.local/share/enclos/sandboxes/pi/home/.hermes/state.db"
DEFAULT = "Hermes · enclos/pi · default"
ALICE = "Hermes · enclos/pi · alice"
MAY_1 = "2026-05-01T00:00:00Z"
OCT_1 = "2026-10-01T00:00:00Z"
ROWS_SINCE_OCT_1 = 5  # rows that started after it; one more started before but grew after
UNKNOWN = "arrival unknown"


def test_enclos_agent_history_counts_from_its_arrival(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox="2026-05-01T00:00:00.123456789Z")
    batch = collect(adapter, agents[DEFAULT], AdapterState())
    assert len(batch.usage) == ROWS
    assert sum(r.output_tokens or 0 for r in batch.usage) == OUTPUT_TOKENS
    assert adapter.health().reason is None


def test_history_from_before_an_import_is_not_counted_again(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=OCT_1)
    state = AdapterState()
    assert len(collect(adapter, agents[DEFAULT], state).usage) == ROWS_SINCE_OCT_1
    grow(tmp_path / SANDBOX_DB, CODEX_SESSION, 500, 1_792_000_000.0)
    (record,) = collect(adapter, agents[DEFAULT], state).usage
    assert record.output_tokens == 500


def test_an_imported_session_going_on_later_counts_its_growth(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox="2026-10-14T11:59:00Z")  # after every row
    state = AdapterState()
    assert collect(adapter, agents[DEFAULT], state).usage == []
    grow(tmp_path / SANDBOX_DB, CODEX_SESSION, 500, 1_800_000_000.0)
    (record,) = collect(adapter, agents[DEFAULT], state).usage
    assert record.output_tokens == 500


def test_arrival_holds_across_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hermes_usage, "ROWS_PER_TICK", 2)
    adapter, agents = in_enclos(tmp_path, sandbox=OCT_1)
    state, counted = AdapterState(), 0
    for _ in range(200):
        batch = collect(adapter, agents[DEFAULT], state)
        counted += len(batch.usage)
        if batch.backlog is not None and batch.backlog.complete:
            break
    assert counted == ROWS_SINCE_OCT_1


def test_each_profile_arrives_as_its_enclos_agent(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=MAY_1, alice="2026-10-01T02:00:00+02:00")
    assert len(collect(adapter, agents[DEFAULT], AdapterState()).usage) == ROWS
    assert len(collect(adapter, agents[ALICE], AdapterState()).usage) == ROWS_SINCE_OCT_1


def test_a_plain_home_is_never_cut_off(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "home" / ".hermes", ignore=NO_PROFILES)
    adapter, agents = in_enclos(tmp_path, sandbox=OCT_1)
    assert len(collect(adapter, agents["Hermes"], AdapterState()).usage) == ROWS


def test_unknown_arrival_counts_from_first_sight(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path)
    state = AdapterState()
    assert collect(adapter, agents[DEFAULT], state).usage == []
    reason = adapter.health().reason or ""
    assert "enclos/pi · default: arrival unknown (enclos's state.db could not be opened)" in reason
    assert "counted from 2026-10-14 12:00 UTC" in reason
    grow(tmp_path / SANDBOX_DB, CODEX_SESSION, 500, 1_800_000_000.0)
    later = fake_context(homes=[], now=datetime(2028, 1, 1, tzinfo=UTC))
    (record,) = adapter.collect(later, agents[DEFAULT], state).usage
    assert record.output_tokens == 500


@pytest.mark.parametrize(
    "created", ["yesterday", "2026-10-01T00:00:00", "9" * 65, "0001-01-01T00:00:00+01:00"]
)
def test_odd_enclos_times_are_unknown(tmp_path: Path, created: str) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=created)
    assert collect(adapter, agents[DEFAULT], AdapterState()).usage == []
    assert "time for the sandbox is unusable" in (adapter.health().reason or "")


def test_an_odd_agent_time_is_unknown_not_the_sandbox_time(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=MAY_1, alice="garbage")
    assert collect(adapter, agents[ALICE], AdapterState()).usage == []
    assert "enclos/pi · alice: arrival unknown (enclos's time for agent alice is unusable)" in (
        adapter.health().reason or ""
    )


def test_a_time_in_the_future_is_unknown(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox="2030-01-01T00:00:00Z")
    assert collect(adapter, agents[DEFAULT], AdapterState()).usage == []
    assert "in the future" in (adapter.health().reason or "")


def test_an_enclos_without_agents_keeps_its_sandbox_times(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=MAY_1, alice=OCT_1)
    with sqlite3.connect(tmp_path / ENCLOS_DB) as conn:
        conn.execute("drop table agents")
    assert len(collect(adapter, agents[DEFAULT], AdapterState()).usage) == ROWS
    assert collect(adapter, agents[ALICE], AdapterState()).usage == []
    assert "agents could not be read" in (adapter.health().reason or "")


def test_a_broken_enclos_state_db_is_unknown(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path)
    (tmp_path / ENCLOS_DB).write_bytes(b"not a database")
    assert collect(adapter, agents[DEFAULT], AdapterState()).usage == []
    assert UNKNOWN in (adapter.health().reason or "")


def test_a_profile_imported_while_running_counts_from_its_arrival(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, wal=True, sandbox=MAY_1)
    collect(adapter, agents[DEFAULT], AdapterState())
    writer = sqlite3.connect(tmp_path / ENCLOS_DB)
    try:
        writer.execute("pragma wal_autocheckpoint=0")
        writer.execute("insert into agents values ('pi', 'bob', ?)", (OCT_1,))
        writer.commit()
        bob = tmp_path / "home/.local/share/enclos/sandboxes/pi/home/.hermes/profiles/bob"
        bob.mkdir()
        shutil.copy(FIXTURE / "state.db", bob / "state.db")
        _, agents = detect_enclos(tmp_path, adapter)
        batch = collect(adapter, agents["Hermes · enclos/pi · bob"], AdapterState())
    finally:
        writer.close()
    assert len(batch.usage) == ROWS_SINCE_OCT_1


def test_a_busy_enclos_state_db_is_read_on_a_later_tick(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=MAY_1)
    lock = sqlite3.connect(tmp_path / ENCLOS_DB, isolation_level=None)
    state = AdapterState()
    try:
        lock.execute("begin exclusive")
        assert collect(adapter, agents[DEFAULT], state).usage == []
        assert "busy: usage is read on a later tick" in (adapter.health().reason or "")
    finally:
        lock.execute("rollback")
        lock.close()
    assert len(collect(adapter, agents[DEFAULT], state).usage) == ROWS


def test_a_known_arrival_is_kept_when_enclos_state_db_goes(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path, sandbox=OCT_1)
    state = AdapterState()
    collect(adapter, agents[DEFAULT], state)
    (tmp_path / ENCLOS_DB).unlink()
    grow(tmp_path / SANDBOX_DB, CODEX_SESSION, 500, 1_800_000_000.0)
    later = fake_context(homes=[], now=datetime(2028, 1, 1, tzinfo=UTC))
    (record,) = adapter.collect(later, agents[DEFAULT], state).usage
    assert record.output_tokens == 500


def test_the_arrival_survives_a_restart(tmp_path: Path) -> None:
    adapter, agents = in_enclos(tmp_path)
    key = f"usage:{agents[DEFAULT].agent_id}"
    state = AdapterState()
    collect(adapter, agents[DEFAULT], state)
    restarted = AdapterState()
    restarted.stage(key, json.loads(json.dumps(state.get(key))))
    restarted.commit()
    grow(tmp_path / SANDBOX_DB, CODEX_SESSION, 500, 1_800_000_000.0)
    adapter, agents = detect_enclos(tmp_path, HermesAdapter())
    later = fake_context(homes=[], now=datetime(2028, 1, 1, tzinfo=UTC))
    (record,) = adapter.collect(later, agents[DEFAULT], restarted).usage
    assert record.output_tokens == 500
    restarted.commit()
    assert (restarted.get(key) or {})["arrived"] == FIXED_NOW.timestamp()


@pytest.mark.parametrize("saved", [float("nan"), True, "x"])
def test_an_odd_saved_arrival_is_ignored(saved: object) -> None:
    assert hermes_usage.Cursor.load({"arrived": saved, "seen": saved}).arrived is None
