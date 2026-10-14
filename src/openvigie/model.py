"""Domain model shared by adapters, collector and hub, plus its JSON codec.

Every value that crosses a process boundary goes through ``to_dict`` / ``from_dict``.
``from_dict`` treats its input as untrusted: types, enum values, string lengths, collection sizes
and integer ranges are all checked. Unknown token counts are ``None`` (JSON ``null``), never 0.
"""

from __future__ import annotations

import dataclasses
import hashlib
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Union, get_args, get_origin, get_type_hints

from openvigie import semconv

SCHEMA_VERSION = 1

MAX_STR = 4096
MAX_ID = 256
MAX_ITEMS = 1024
MAX_INT = 2**53


class AgentStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    STOPPED = "stopped"
    UNKNOWN = "unknown"


class SandboxState(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class UsageQuality(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


class SpanStatus(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


def _id() -> Any:
    return field(metadata={"max_len": MAX_ID})


def stable_id(*parts: str) -> str:
    """Deterministic 128-bit hex id from its parts (used for agents, records, traces)."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return digest[:32]


@dataclass(frozen=True, slots=True)
class Host:
    host_id: str = _id()
    hostname: str = _id()
    os: str = _id()
    arch: str = _id()
    collector_version: str = _id()


@dataclass(frozen=True, slots=True)
class AgentInstance:
    agent_id: str = _id()
    host_id: str = _id()
    agent_type: str = _id()
    name: str = _id()
    version: str | None = None
    config_root: str | None = None
    sandboxed: SandboxState = SandboxState.UNKNOWN
    sandbox_kind: str | None = None
    process_running: bool | None = None
    last_activity_at: datetime | None = None
    status: AgentStatus = AgentStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class StatusEvent:
    agent_id: str = _id()
    old: AgentStatus = AgentStatus.UNKNOWN
    new: AgentStatus = AgentStatus.UNKNOWN
    at: datetime = field(default_factory=lambda: datetime.fromtimestamp(0).astimezone())


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str = _id()
    agent_id: str = _id()
    started_at: datetime | None = None
    last_event_at: datetime | None = None
    models: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Token usage for one model call. Token fields are ``None`` when unknown."""

    record_id: str = _id()
    agent_id: str = _id()
    timestamp: datetime = field(default_factory=lambda: datetime.fromtimestamp(0).astimezone())
    session_id: str | None = None
    provider: str | None = None
    request_model: str | None = None
    response_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    # Anthropic: the part of cache_creation_input_tokens written with the 1-hour TTL (billed 2x
    # input instead of 1.25x). None when the provider doesn't say.
    cache_creation_1h_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    quality: UsageQuality = UsageQuality.UNKNOWN

    def semconv_attributes(self) -> dict[str, str | int]:
        """OTel GenAI attributes for this record; unknown values are omitted, not zeroed."""
        pairs: list[tuple[str, str | int | None]] = [
            (semconv.GEN_AI_PROVIDER_NAME, self.provider),
            (semconv.GEN_AI_REQUEST_MODEL, self.request_model),
            (semconv.GEN_AI_RESPONSE_MODEL, self.response_model),
            (semconv.GEN_AI_USAGE_INPUT_TOKENS, self.input_tokens),
            (semconv.GEN_AI_USAGE_OUTPUT_TOKENS, self.output_tokens),
            (semconv.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, self.cache_read_input_tokens),
            (semconv.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, self.cache_creation_input_tokens),
            (semconv.OPENVIGIE_USAGE_REASONING_TOKENS, self.reasoning_tokens),
            (semconv.SESSION_ID, self.session_id),
            (semconv.OPENVIGIE_USAGE_QUALITY, self.quality.value),
        ]
        return {k: v for k, v in pairs if v is not None}


AttrValue = str | int | float | bool


@dataclass(frozen=True, slots=True)
class Span:
    trace_id: str = field(metadata={"max_len": 32})
    span_id: str = field(metadata={"max_len": 16})
    name: str = _id()
    start_time: datetime = field(default_factory=lambda: datetime.fromtimestamp(0).astimezone())
    end_time: datetime | None = None
    parent_span_id: str | None = None
    agent_id: str | None = None
    status: SpanStatus = SpanStatus.UNSET
    attributes: Mapping[str, AttrValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    adapter: str = _id()
    state: HealthState = HealthState.OK
    reason: str | None = None
    parse_errors: int = 0
    skipped: int = 0


# --- codec -------------------------------------------------------------------------------------


class CodecError(ValueError):
    """Raised by ``from_dict`` on any invalid input. Messages never echo the input value."""


def to_dict(obj: Any) -> dict[str, Any]:
    if (
        not dataclasses.is_dataclass(obj)
        or isinstance(obj, type)
        or type(obj).__module__ != __name__
    ):
        # only wire types are serializable (e.g. never ProcessInfo with its command line)
        raise TypeError("to_dict only serializes openvigie.model types")
    return {f.name: _encode(getattr(obj, f.name)) for f in dataclasses.fields(obj)}


def _encode(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_encode(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _encode(v) for k, v in value.items()}
    return value


def from_dict[T](cls: type[T], data: Any) -> T:
    if not isinstance(data, Mapping):
        raise CodecError(f"{cls.__name__}: expected an object")
    hints = get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - set(fields)
    if unknown:
        raise CodecError(f"{cls.__name__}: unknown field(s) {sorted(unknown)[:5]}")
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        where = f"{cls.__name__}.{name}"
        if name not in data:
            has_default = (
                f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
            )
            if not has_default:
                raise CodecError(f"{where}: missing")
            continue
        max_len = int(f.metadata.get("max_len", MAX_STR))
        kwargs[name] = _decode(hints[name], data[name], where, max_len)
    return cls(**kwargs)


def _decode(tp: Any, value: Any, where: str, max_len: int) -> Any:
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        options = get_args(tp)
        if value is None:
            if type(None) in options:
                return None
            raise CodecError(f"{where}: must not be null")
        non_none = [t for t in options if t is not type(None)]
        if len(non_none) == 1:
            return _decode(non_none[0], value, where, max_len)
        return _decode_scalar_union(non_none, value, where, max_len)
    if origin is tuple:
        (item_tp, _) = get_args(tp)
        if not isinstance(value, list) or len(value) > MAX_ITEMS:
            raise CodecError(f"{where}: expected a list of at most {MAX_ITEMS} items")
        return tuple(_decode(item_tp, v, where, max_len) for v in value)
    if origin is Mapping or tp is Mapping:
        if not isinstance(value, Mapping) or len(value) > MAX_ITEMS:
            raise CodecError(f"{where}: expected an object of at most {MAX_ITEMS} entries")
        _, val_tp = get_args(tp)
        return {
            _decode(str, k, where, MAX_ID): _decode(val_tp, v, where, max_len)
            for k, v in value.items()
        }
    if value is None:
        raise CodecError(f"{where}: must not be null")
    return _decode_scalar(tp, value, where, max_len)


def _decode_scalar_union(options: list[Any], value: Any, where: str, max_len: int) -> Any:
    for tp in options:
        if _matches(tp, value):
            return _decode_scalar(tp, value, where, max_len)
    raise CodecError(f"{where}: unexpected type")


def _matches(tp: Any, value: Any) -> bool:
    if tp is bool:
        return isinstance(value, bool)
    if tp is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if tp is float:
        return isinstance(value, float)
    return isinstance(value, str) if tp is str else False


def _decode_scalar(tp: Any, value: Any, where: str, max_len: int) -> Any:
    if isinstance(tp, type) and issubclass(tp, StrEnum):
        try:
            return tp(value)
        except ValueError:
            raise CodecError(f"{where}: invalid value") from None
    if tp is datetime:
        if not isinstance(value, str) or len(value) > 64:
            raise CodecError(f"{where}: expected an ISO 8601 string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise CodecError(f"{where}: invalid timestamp") from None
        if parsed.tzinfo is None:
            raise CodecError(f"{where}: timestamp needs a timezone")
        return parsed
    if tp is str:
        if not isinstance(value, str):
            raise CodecError(f"{where}: expected a string")
        if len(value) > max_len:
            raise CodecError(f"{where}: longer than {max_len} characters")
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise CodecError(f"{where}: expected a boolean")
        return value
    if tp is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise CodecError(f"{where}: expected an integer")
        if not 0 <= value <= MAX_INT:
            raise CodecError(f"{where}: out of range")
        return value
    if tp is float:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise CodecError(f"{where}: expected a number")
        return float(value)
    raise CodecError(f"{where}: unsupported type")
