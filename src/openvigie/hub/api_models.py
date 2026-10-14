"""Request and response bodies of the hub API (``/api/v1``), PRD D12.

Endpoints declare these models, so FastAPI validates and documents them. The records inside a push
are the wire types of ``openvigie.model``. ``describe_invalid`` turns validation errors into the
API's error messages, which never echo input values.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, Strict

from openvigie.hub.reports import UsageRow
from openvigie.model import (
    AdapterHealth,
    AgentInstance,
    Capabilities,
    Host,
    Id,
    OpenShellSandbox,
    Session,
    Span,
    StatusEvent,
    Timestamp,
    UsageRecord,
)
from openvigie.risk import Flag


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


# --- POST /api/v1/enrol ------------------------------------------------------------------------


class EnrolRequest(ApiModel):
    code: Annotated[str, Strict(), Field(max_length=128)]
    host: Host


class EnrolResponse(ApiModel):
    host_id: Id
    token: str


# --- POST /api/v1/ingest -----------------------------------------------------------------------


class CollectorInfo(ApiModel):
    model_config = ConfigDict(extra="ignore")  # newer collectors may report more

    spool_dropped: Annotated[int, Strict(), Field(ge=0, lt=2**31)] | None = None


class IngestRequest(ApiModel):
    """One push from a collector. Unknown sections are accepted and reported back as ``ignored``,
    so a newer collector learns what an older hub didn't store."""

    model_config = ConfigDict(extra="allow")

    schema_version: Annotated[int, Strict()]
    host: Host
    sent_at: Timestamp
    collector: CollectorInfo = CollectorInfo()
    # sections: lists of records, with a maximum per push (more gets a 413)
    agents: Annotated[list[AgentInstance], Field(max_length=500)] = []
    status_events: Annotated[list[StatusEvent], Field(max_length=5000)] = []
    adapter_health: Annotated[list[AdapterHealth], Field(max_length=64)] = []
    usage: Annotated[list[UsageRecord], Field(max_length=5000)] = []
    sessions: Annotated[list[Session], Field(max_length=1000)] = []
    spans: Annotated[list[Span], Field(max_length=2000)] = []
    capabilities: Annotated[list[Capabilities], Field(max_length=500)] = []
    # the host's full set of OpenShell sandboxes; absent: not reported, []: none any more
    sandboxes: Annotated[list[OpenShellSandbox], Field(max_length=500)] | None = None


class Accepted(ApiModel):
    """How many records of each section were stored (duplicates and replays are not)."""

    agents: int
    status_events: int
    adapter_health: int
    usage: int
    sessions: int
    capabilities: int
    sandboxes: int = 0
    spans: int


class TracesWanted(ApiModel):
    """Tells collectors whether to build and send spans (FR-6)."""

    wanted: bool
    export_content: bool


class IngestResponse(ApiModel):
    schema_version: int
    accepted: Accepted
    ignored: list[str]
    next_push_s: int
    traces: TracesWanted


# --- GET /api/v1/agents ------------------------------------------------------------------------


class FlagOut(ApiModel):
    id: str
    severity: str  # high | medium | low
    title: str
    reason: str

    @classmethod
    def of(cls, flag: Flag) -> FlagOut:
        return cls(id=flag.rule_id, severity=flag.severity.label, title=flag.title,
                   reason=flag.reason)  # fmt: skip


class RiskOut(ApiModel):
    level: str  # high | medium | low | none | unknown
    flags: list[FlagOut]


class AgentOut(ApiModel):
    agent: AgentInstance
    hostname: str
    risk: RiskOut


class SandboxesResponse(ApiModel):
    """``GET /api/v1/sandboxes``: every OpenShell sandbox the hub knows, by host (FR-10)."""

    sandboxes: list[OpenShellSandbox]


class AgentsResponse(ApiModel):
    agents: list[AgentOut]
    rule_problems: list[str]  # e.g. a broken risk.toml override


# --- GET /api/v1/usage -------------------------------------------------------------------------


class Tokens(ApiModel):
    input: int
    output: int
    cache_read: int
    cache_write: int


class Coverage(ApiModel):
    exact: int
    estimated: int
    unknown: int
    unpriced_tokens: int


class UsageRowOut(ApiModel):
    key: dict[str, str]
    labels: dict[str, str]
    records: int
    tokens: Tokens
    coverage: Coverage
    cost_usd: str  # a decimal string, to keep precision
    cost_complete: bool  # false: part of the usage couldn't be priced, cost is a lower bound
    unverified_price_models: list[str]

    @classmethod
    def of(cls, row: UsageRow) -> UsageRowOut:
        return cls(
            key=row.key,
            labels=row.labels,
            records=row.records,
            tokens=Tokens(input=row.input_tokens, output=row.output_tokens,
                          cache_read=row.cache_read_tokens, cache_write=row.cache_write_tokens),
            coverage=Coverage(exact=row.exact, estimated=row.estimated, unknown=row.unknown,
                              unpriced_tokens=row.unpriced_tokens),
            cost_usd=row.cost_usd,
            cost_complete=row.cost_complete,
            unverified_price_models=sorted(row.unverified_models),
        )  # fmt: skip


class UsageResponse(ApiModel):
    start: Timestamp = Field(serialization_alias="from")
    end: Timestamp = Field(serialization_alias="to")
    group_by: list[str]
    rows: list[UsageRowOut]
    total: UsageRowOut


# --- errors ------------------------------------------------------------------------------------


def describe_invalid(errors: list[Any]) -> tuple[int, str]:
    """HTTP status and message for a request that failed validation: the first error, located,
    never quoting the input. A section with too many records is a 413."""
    first = errors[0]
    loc = [str(part) for part in first["loc"] if part != "body"]
    kind = first["type"]
    if kind == "json_invalid":
        return 422, "body must be JSON"
    if kind == "too_long" and len(loc) == 1 and loc[0] in IngestRequest.model_fields:
        return 413, f"{loc[0]}: at most {first['ctx']['max_length']} items per push"
    message = {
        "missing": "missing",
        "extra_forbidden": "unknown field",
        "unexpected_keyword_argument": "unknown field",
    }.get(kind, first["msg"])
    return 422, f"{'.'.join(loc) or 'body'}: {message}"
