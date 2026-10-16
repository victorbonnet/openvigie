import json
import shutil
from pathlib import Path
from typing import Any

from openvigie.adapters.base import AdapterState
from openvigie.adapters.codex import CodexAdapter
from openvigie.adapters.testing import fake_context
from openvigie.model import AgentInstance, UsageQuality, UsageRecord, stable_id

FIXTURE = Path(__file__).parent / "fixtures" / "home" / ".codex"
S1 = "0199e5a0-1111-7000-8000-000000000001"
S2 = "0199e0b0-2222-7000-8000-000000000002"
S3 = "0199dc00-3333-7000-8000-000000000003"


def setup(tmp_path: Path) -> tuple[CodexAdapter, AgentInstance, Any, Path]:
    home = tmp_path / "home"
    shutil.copytree(FIXTURE, home / ".codex")
    ctx = fake_context(homes=[home])
    adapter = CodexAdapter()
    (agent,) = adapter.detect(ctx)
    return adapter, agent, ctx, home / ".codex"


def sums(records: list[UsageRecord], session: str) -> tuple[int, int, int, int]:
    mine = [r for r in records if r.session_id == session]
    return (
        sum(r.input_tokens or 0 for r in mine),
        sum(r.cache_read_input_tokens or 0 for r in mine),
        sum(r.output_tokens or 0 for r in mine),
        sum(r.reasoning_tokens or 0 for r in mine),
    )


def test_fixture_totals_match_manifest(tmp_path: Path) -> None:
    adapter, agent, ctx, _ = setup(tmp_path)
    batch = adapter.collect(ctx, agent, AdapterState())
    assert sums(batch.usage, S1) == (12000, 4800, 650, 128)
    assert sums(batch.usage, S2) == (3000, 0, 120, 0)
    assert len([r for r in batch.usage if r.session_id == S1]) == 2  # duplicate event skipped
    assert {r.response_model for r in batch.usage} == {"gpt-5-codex", "gpt-5", "gpt-6.1-sol"}
    assert all(r.provider == "openai" and r.quality is UsageQuality.EXACT for r in batch.usage)
    assert {s.session_id: s.models for s in batch.sessions} == {
        S1: ("gpt-5-codex",),
        S2: ("gpt-5",),
        S3: ("gpt-6.1-sol",),
    }


def test_cache_writes_are_mapped_per_turn(tmp_path: Path) -> None:
    adapter, agent, ctx, _ = setup(tmp_path)
    batch = adapter.collect(ctx, agent, AdapterState())
    mine = [r for r in batch.usage if r.session_id == S3]
    tokens = [
        (r.input_tokens, r.cache_read_input_tokens, r.cache_creation_input_tokens, r.output_tokens)
        for r in mine
    ]
    assert tokens == [(10000, 0, 6000, 300), (12000, 9000, 2000, 200)]
    # rollouts written before Codex recorded cache writes leave them unset
    assert {r.cache_creation_input_tokens for r in batch.usage if r.session_id != S3} == {None}


def test_record_ids_ignore_cache_writes(tmp_path: Path) -> None:
    """Ids hash the four original totals, so records read before cache writes were mapped keep
    their id and are not stored twice."""
    adapter, agent, ctx, _ = setup(tmp_path)
    usage = adapter.collect(ctx, agent, AdapterState()).usage
    first = next(r for r in usage if r.session_id == S3)
    assert first.record_id == stable_id(agent.agent_id, S3, "10000", "0", "300", "64")


def test_incremental_reads_keep_running_totals(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    state = AdapterState()
    adapter.collect(ctx, agent, state)
    state.commit()
    rollout = next((root / "sessions" / "2026" / "10" / "14").glob("rollout-*.jsonl"))
    total = {"input_tokens": 15000, "cached_input_tokens": 6000, "output_tokens": 700,
             "reasoning_output_tokens": 128, "total_tokens": 15700}  # fmt: skip
    event = {"timestamp": "2026-10-14T09:01:00.000Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": total}}}  # fmt: skip
    with rollout.open("a") as fh:
        fh.write(json.dumps(event) + "\n")
    (record,) = adapter.collect(ctx, agent, state).usage
    assert (record.input_tokens, record.cache_read_input_tokens, record.output_tokens) == (
        3000,
        1200,
        50,
    )
    assert record.response_model == "gpt-5-codex" and record.session_id == S1
    state.commit()
    assert adapter.collect(ctx, agent, state).usage == []


def test_provider_from_config(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    (root / "config.toml").write_text('model_provider = "openrouter"\n')
    (agent,) = adapter.detect(ctx)
    assert {r.provider for r in adapter.collect(ctx, agent, AdapterState()).usage} == {"openrouter"}


def test_counter_reset_falls_back_to_last_turn(tmp_path: Path) -> None:
    adapter, agent, ctx, root = setup(tmp_path)
    path = root / "sessions" / "2026" / "10" / "15" / "rollout-x.jsonl"
    path.parent.mkdir(parents=True)

    def count(total: int, last: int) -> dict[str, Any]:
        def mk(n: int) -> dict[str, int]:
            return {"input_tokens": n, "output_tokens": n}

        return {"timestamp": "2026-10-15T10:00:00Z", "type": "event_msg",
                "payload": {"type": "token_count",
                            "info": {"total_token_usage": mk(total),
                                     "last_token_usage": mk(last)}}}  # fmt: skip

    meta = {"timestamp": "2026-10-15T10:00:00Z", "type": "session_meta", "payload": {"id": "s3"}}
    path.write_text("".join(json.dumps(x) + "\n" for x in [meta, count(100, 100), count(30, 30)]))
    records = [r for r in adapter.collect(ctx, agent, AdapterState()).usage if r.session_id == "s3"]
    assert [r.input_tokens for r in records] == [100, 30]
