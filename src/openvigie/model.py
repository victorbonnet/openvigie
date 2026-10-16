"""Domain model shared by adapters, collector and hub: the collector ↔ hub wire format.

Every type here is a frozen Pydantic dataclass (PRD D12): building one validates it, so an invalid
object can't exist. Validation is strict where it matters: strings, numbers and booleans are never
coerced, timestamps are ISO 8601 strings (or datetimes) with an offset, enums take their values;
strings, lists and integers are bounded, unknown fields are rejected, and errors never echo the
input. ``to_dict`` / ``from_dict`` convert to and from the JSON wire format. Unknown token counts
are ``None`` (JSON ``null``), never 0.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self, TypeVar, cast, dataclass_transform

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    Strict,
    StrictFloat,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic.dataclasses import dataclass

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


CONFIG = ConfigDict(extra="forbid", hide_input_in_errors=True)


@dataclass_transform(frozen_default=True, field_specifiers=(field, Field))
def wire_type[C](cls: type[C]) -> type[C]:
    """Decorator for a wire type: a frozen Pydantic dataclass that rejects unknown fields."""
    return cast(type[C], dataclass(frozen=True, slots=True, config=CONFIG)(cls))


def _iso_string(value: object) -> object:
    if isinstance(value, datetime) or (isinstance(value, str) and len(value) <= 64):
        return value
    raise ValueError("expected an ISO 8601 timestamp")  # not a number of seconds


T = TypeVar("T")
Id = Annotated[str, Strict(), Field(max_length=MAX_ID)]
Text = Annotated[str, Strict(), Field(max_length=MAX_STR)]
Count = Annotated[int, Strict(), Field(ge=0, le=MAX_INT)]
Bool = Annotated[bool, Strict()]
Items = Annotated[tuple[T, ...], Field(max_length=MAX_ITEMS)]
# ISO 8601 with an explicit offset ("+00:00", not "Z"): the format of SCHEMA_VERSION 1.
Timestamp = Annotated[
    AwareDatetime,
    BeforeValidator(_iso_string),
    PlainSerializer(datetime.isoformat, return_type=str),
]
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def stable_id(*parts: str) -> str:
    """Deterministic 128-bit hex id from its parts (used for agents, records, traces)."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return digest[:32]


@wire_type
class Host:
    host_id: Id
    hostname: Id
    os: Id
    arch: Id
    collector_version: Id


@wire_type
class AgentInstance:
    agent_id: Id
    host_id: Id
    agent_type: Id
    name: Id
    version: Text | None = None
    config_root: Text | None = None
    sandboxed: SandboxState = SandboxState.UNKNOWN
    sandbox_kind: Text | None = None
    process_running: Bool | None = None
    last_activity_at: Timestamp | None = None
    status: AgentStatus = AgentStatus.UNKNOWN


@wire_type
class StatusEvent:
    agent_id: Id
    old: AgentStatus = AgentStatus.UNKNOWN
    new: AgentStatus = AgentStatus.UNKNOWN
    at: Timestamp = EPOCH


@wire_type
class Session:
    session_id: Id
    agent_id: Id
    started_at: Timestamp | None = None
    last_event_at: Timestamp | None = None
    models: Items[Text] = ()


@wire_type
class UsageRecord:
    """Token usage for one model call. Token fields are ``None`` when unknown."""

    record_id: Id
    agent_id: Id
    timestamp: Timestamp = EPOCH
    session_id: Text | None = None
    provider: Text | None = None
    request_model: Text | None = None
    response_model: Text | None = None
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cache_read_input_tokens: Count | None = None
    cache_creation_input_tokens: Count | None = None
    # Anthropic: the part of cache_creation_input_tokens written with the 1-hour TTL (billed 2x
    # input instead of 1.25x). None when the provider doesn't say.
    cache_creation_1h_input_tokens: Count | None = None
    reasoning_tokens: Count | None = None
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


@wire_type
class UsageBackfill:
    """How far the collector has read an agent's usage logs (FR-3).

    While ``complete`` is false, usage is still being read: any of the agent's totals may be
    missing records, which are unknown, not zero.
    """

    agent_id: Id
    observed_at: Timestamp = EPOCH
    complete: Bool = True
    # every log file last written before this has been read; None when complete. Not a bound
    # on the records left: a file written later can still hold older records (resumed sessions).
    read_until: Timestamp | None = None
    bytes_left: Count | None = None  # None: not known

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.complete and (self.read_until is not None or self.bytes_left):
            raise ValueError("a complete backfill has nothing left to read")
        return self


AttrValue = Text | Annotated[int, Strict(), Field(ge=-MAX_INT, le=MAX_INT)] | StrictFloat | Bool


@wire_type
class Span:
    trace_id: Annotated[str, Field(max_length=32)]
    span_id: Annotated[str, Field(max_length=16)]
    name: Id
    start_time: Timestamp = EPOCH
    end_time: Timestamp | None = None
    parent_span_id: Text | None = None
    agent_id: Text | None = None
    status: SpanStatus = SpanStatus.UNSET
    attributes: Annotated[dict[Id, AttrValue], Field(max_length=MAX_ITEMS)] = field(
        default_factory=dict
    )


class ApprovalLevel(StrEnum):
    """How much an agent asks before acting, normalised across agents (FR-8)."""

    ASK = "ask"  # asks before edits and commands
    AUTO_EDIT = "auto_edit"  # edits files without asking, asks for commands
    NEVER_ASK = "never_ask"  # runs anything without asking (bypass / yolo modes)
    UNKNOWN = "unknown"


class FsAccess(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE = "workspace"  # writes limited to the project / writable roots
    FULL = "full"
    UNKNOWN = "unknown"


class NetAccess(StrEnum):
    NONE = "none"
    ALLOWLIST = "allowlist"
    FULL = "full"
    UNKNOWN = "unknown"


@wire_type
class McpServer:
    name: Id
    transport: Id  # "stdio" | "http" | "sse" | other
    command: Id | None = None  # first word only
    url_host: Id | None = None
    pinned: Bool | None = None  # package version pinned (npx pkg@1.2.3); None: not a package
    secret_names: Items[Text] = ()  # env var names passed to it, never values


@wire_type
class Hook:
    event: Id
    command: Id | None = None  # first word only


@wire_type
class SecretRef:
    """A secret an agent's tools can reach, by name only: values are never read into the model."""

    name: Id
    where: Id  # e.g. "settings.json env", "mcp:github"


@wire_type
class Capabilities:
    """What an agent can access (FR-8). ``None`` and ``UNKNOWN`` mean unknown, never "none"."""

    agent_id: Id
    observed_at: Timestamp = EPOCH
    approvals: ApprovalLevel = ApprovalLevel.UNKNOWN
    approval_mode: Id | None = None  # agent's term
    filesystem: FsAccess = FsAccess.UNKNOWN
    writable_roots: Items[Text] = ()
    network: NetAccess = NetAccess.UNKNOWN
    network_allowlist: Items[Text] = ()
    shell: Bool | None = None  # can run shell commands
    tools_allowed: Items[Text] = ()
    tools_denied: Items[Text] = ()
    mcp_servers: Items[McpServer] = ()
    skills: Items[Text] = ()
    hooks: Items[Hook] = ()
    secrets: Items[SecretRef] = ()
    sources: Items[Text] = ()  # config files read, as display paths


@wire_type
class AdapterHealth:
    adapter: Id
    state: HealthState = HealthState.OK
    reason: Text | None = None
    parse_errors: Count = 0
    skipped: Count = 0


@wire_type
class SandboxEndpoint:
    """A destination a sandbox's network rule allows (OpenShell ``network_policies``)."""

    host: Id | None = None  # None when the rule only lists allowed IPs
    ports: Items[Count] = ()
    path: Text | None = None
    protocol: Id | None = None  # rest | websocket | graphql | mcp | json-rpc | tcp
    access: Id | None = None  # read-only | read-write | full
    enforcement: Id | None = None  # enforce | audit; None means OpenShell's default (audit)


@wire_type
class SandboxRule:
    name: Id
    endpoints: Items[SandboxEndpoint] = ()
    binaries: Items[Text] = ()  # executables allowed to use the endpoints


@wire_type
class OpenShellSandbox:
    """One OpenShell sandbox on a host and its active policy's network rules (FR-10)."""

    sandbox_id: Id
    host_id: Id
    name: Id
    observed_at: Timestamp = EPOCH
    workspace: Id = "default"
    phase: Id = ""
    labels: Annotated[dict[Id, Text], Field(max_length=MAX_ITEMS)] = field(default_factory=dict)
    agent_type: Id | None = None  # from the label openvigie.agent=<type>
    agent_ids: Items[Id] = ()  # detected agents running in it
    policy_version: Count | None = None
    policy_hash: Id | None = None
    network_rules: Items[SandboxRule] | None = None  # None: the policy couldn't be read
    # the exact network_policies as canonical JSON, to edit and roll back (FR-12); None: unknown
    network_policy: Annotated[str, Strict(), Field(max_length=64 * 1024)] | None = None


# --- commands (D14) ----------------------------------------------------------------------------

MAX_COMMAND_ARGS_CHARS = 64 * 1024  # all of one command's argument values together
CommandId = Annotated[str, Strict(), Field(pattern=r"^[0-9a-f]{32}$")]
ActionName = Annotated[str, Strict(), Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
CommandArg = Annotated[str, Strict(), Field(max_length=MAX_COMMAND_ARGS_CHARS)]


class CommandStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"  # not run: not allowed here, expired, replayed or invalid


@wire_type
class Command:
    """An action the hub asks one collector to run, sent in the ingest reply (D14)."""

    command_id: CommandId
    action: ActionName
    expires_at: Timestamp
    args: Annotated[dict[Id, CommandArg], Field(max_length=8)] = field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded(self) -> Self:
        if sum(len(v) for v in self.args.values()) > MAX_COMMAND_ARGS_CHARS:
            raise ValueError(f"arguments over {MAX_COMMAND_ARGS_CHARS} characters in all")
        return self


@wire_type
class CommandResult:
    """What became of a command, reported by the collector in its next push."""

    command_id: CommandId
    status: CommandStatus
    finished_at: Timestamp
    detail: Text = ""


# --- codec -------------------------------------------------------------------------------------


class CodecError(ValueError):
    """Raised by ``from_dict`` on any invalid input. Messages never echo the input value."""


_ADAPTERS: dict[type[Any], TypeAdapter[Any]] = {}


def _adapter(cls: type[Any]) -> TypeAdapter[Any]:
    if cls not in _ADAPTERS:
        _ADAPTERS[cls] = TypeAdapter(cls)
    return _ADAPTERS[cls]


def to_dict(obj: Any) -> dict[str, Any]:
    """The JSON-ready form of a wire type."""
    if type(obj).__module__ != __name__ or not dataclasses.is_dataclass(obj):
        # only wire types are serializable (e.g. never ProcessInfo with its command line)
        raise TypeError("to_dict only serializes openvigie.model types")
    data: dict[str, Any] = _adapter(type(obj)).dump_python(obj, mode="json")
    return data


def from_dict[M](cls: type[M], data: Any) -> M:
    """Validate untrusted JSON data (as parsed by ``json.loads``) into a wire type."""
    if not isinstance(data, dict):
        raise CodecError(f"{cls.__name__}: expected an object")
    try:
        result: M = _adapter(cls).validate_python(data)
    except ValidationError as exc:
        raise CodecError(_describe(cls, exc)) from None
    return result


def _describe(cls: type[Any], exc: ValidationError) -> str:
    first = exc.errors(include_input=False, include_url=False, include_context=False)[0]
    where = ".".join([cls.__name__, *map(str, first["loc"])])
    message = {"missing": "missing", "unexpected_keyword_argument": "unknown field"}.get(
        first["type"]
    )
    return f"{where}: {message or first['msg']}"
