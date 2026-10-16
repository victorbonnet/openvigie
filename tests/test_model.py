import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from openvigie import semconv
from openvigie.model import (
    EPOCH,
    MAX_ID,
    AdapterHealth,
    AgentInstance,
    AgentStatus,
    CodecError,
    HealthState,
    Host,
    SandboxState,
    Session,
    Span,
    StatusEvent,
    UsageQuality,
    UsageRecord,
    from_dict,
    stable_id,
    to_dict,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

SAMPLES: list[Any] = [
    Host("h1", "pi5", "linux", "aarch64", "0.0.1"),
    AgentInstance(
        agent_id="a1",
        host_id="h1",
        agent_type="claude-code",
        name="Claude Code",
        version="2.1.0",
        config_root="~/.claude",
        sandboxed=SandboxState.YES,
        sandbox_kind="bwrap",
        process_running=True,
        last_activity_at=NOW,
        status=AgentStatus.ACTIVE,
    ),
    StatusEvent("a1", AgentStatus.IDLE, AgentStatus.ACTIVE, NOW),
    Session("s1", "a1", NOW, NOW, ("claude-sonnet-4-5", "claude-haiku-4-5")),
    UsageRecord(
        record_id="r1",
        agent_id="a1",
        timestamp=NOW,
        session_id="s1",
        provider="anthropic",
        request_model="claude-sonnet-4-5",
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=0,
        quality=UsageQuality.EXACT,
    ),
    Span(
        trace_id="0" * 32,
        span_id="1" * 16,
        name="chat claude-sonnet-4-5",
        start_time=NOW,
        end_time=NOW,
        attributes={"gen_ai.usage.input_tokens": 10, "ok": True, "ratio": 0.5, "s": "x"},
    ),
    AdapterHealth("codex", HealthState.DEGRADED, "bad config", parse_errors=2),
]


@pytest.mark.parametrize("obj", SAMPLES, ids=lambda o: type(o).__name__)
def test_round_trip_through_json(obj: Any) -> None:
    wire = json.loads(json.dumps(to_dict(obj)))
    assert from_dict(type(obj), wire) == obj


def test_unknown_tokens_serialize_as_null_not_zero() -> None:
    record = UsageRecord(record_id="r", agent_id="a", timestamp=NOW)
    wire = to_dict(record)
    assert wire["input_tokens"] is None
    assert wire["output_tokens"] is None
    assert wire["quality"] == "unknown"


def test_semconv_attributes_omit_unknown_values() -> None:
    record = UsageRecord(record_id="r", agent_id="a", timestamp=NOW, input_tokens=5)
    attrs = record.semconv_attributes()
    assert attrs[semconv.GEN_AI_USAGE_INPUT_TOKENS] == 5
    assert semconv.GEN_AI_USAGE_OUTPUT_TOKENS not in attrs


def _usage(**overrides: Any) -> dict[str, Any]:
    base = to_dict(UsageRecord(record_id="r", agent_id="a", timestamp=NOW))
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "overrides",
    [
        {"input_tokens": -1},
        {"input_tokens": 2**60},
        {"input_tokens": True},
        {"input_tokens": "10"},
        {"quality": "guessed"},
        {"record_id": "x" * (MAX_ID + 1)},
        {"timestamp": "2026-10-14T12:00:00"},  # no timezone
        {"timestamp": "yesterday"},
        {"timestamp": 1760000000},  # seconds are not a timestamp on the wire
        {"input_tokens": 12.0},
        {"provider": 7},
        {"agent_id": None},
        {"surprise": 1},
    ],
)
def test_from_dict_rejects_invalid_input(overrides: dict[str, Any]) -> None:
    with pytest.raises(CodecError):
        from_dict(UsageRecord, _usage(**overrides))


def test_from_dict_rejects_missing_required_field() -> None:
    data = _usage()
    del data["agent_id"]
    with pytest.raises(CodecError, match="missing"):
        from_dict(UsageRecord, data)


def test_from_dict_rejects_non_object() -> None:
    with pytest.raises(CodecError):
        from_dict(Host, ["h1"])


def test_errors_do_not_echo_input() -> None:
    secret = "sk-ant-api03-" + "A" * 40
    with pytest.raises(CodecError) as exc:
        from_dict(UsageRecord, _usage(quality=secret))
    assert secret not in str(exc.value)


def test_oversized_collections_rejected() -> None:
    data = to_dict(Session("s", "a"))
    data["models"] = ["m"] * 5000
    with pytest.raises(CodecError):
        from_dict(Session, data)


def test_stable_id_is_deterministic_and_separates_parts() -> None:
    assert stable_id("h", "claude-code", "/home/a/.claude") == stable_id(
        "h", "claude-code", "/home/a/.claude"
    )
    assert stable_id("ab", "c") != stable_id("a", "bc")
    assert len(stable_id("x")) == 32


def test_building_a_wire_type_validates_it() -> None:
    # An invalid object can't exist, so it fails in the adapter, not as a rejected push.
    with pytest.raises(ValidationError):
        UsageRecord("r", "a", EPOCH, input_tokens=-1)
    with pytest.raises(ValidationError):
        Host("h" * (MAX_ID + 1), "pi5", "linux", "aarch64", "0.1.0")
    with pytest.raises(ValidationError):
        Session("s", "a", datetime(2026, 10, 14))  # no timezone
    with pytest.raises(ValidationError):
        AgentInstance("a", "h", "codex", "Codex", process_running="yes")  # type: ignore[arg-type]


def test_a_complete_backfill_has_nothing_left() -> None:
    from openvigie.model import CodecError, UsageBackfill, from_dict

    at = datetime(2026, 10, 14, tzinfo=UTC)
    assert UsageBackfill("a1", at, False, None, None).bytes_left is None  # unknown amount
    assert UsageBackfill("a1", at, True, None, 0).complete
    for bad in ({"bytes_left": 5}, {"read_until": at.isoformat()}):
        with pytest.raises(CodecError):
            from_dict(UsageBackfill, {"agent_id": "a1", "complete": True, **bad})
