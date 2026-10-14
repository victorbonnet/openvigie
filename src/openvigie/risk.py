"""Rule-based risk flags (FR-9, PRD D10): no numeric score, every flag explained.

Rules are data (``risk.toml`` shipped with the package, plus ``<config dir>/risk.toml`` to add
or replace rules by id). A rule matches a small set of *facts* about an agent through a tiny
condition language: ``{field, equals}``, ``{field, in}``, ``{field, not_equals}``,
``{field, nonempty}``, and ``all`` / ``any`` / ``not`` over those. Rule files are parsed into the
typed models below (PRD D12); nothing in them is ever evaluated as code. A broken override file
is reported and ignored: the bundled rules stay.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from enum import IntEnum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BeforeValidator,
    Discriminator,
    Field,
    Strict,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    Tag,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from openvigie.config import config_dir, describe, file_entry
from openvigie.model import AgentInstance, Capabilities, FsAccess
from openvigie.safeio import UnsafeFileError, load_toml_bounded

log = logging.getLogger(__name__)

MAX_RULES = 200
MAX_DEPTH = 8
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return self.name.lower()


def _severity(value: object) -> object:
    if isinstance(value, str):
        if value not in ("low", "medium", "high"):
            raise ValueError("severity must be low, medium or high")
        return Severity[value.upper()]
    return value


# --- the rule file format ----------------------------------------------------------------------

Scalar = StrictBool | StrictInt | StrictFloat | StrictStr


@file_entry
class Test:
    """``{ field = "network", equals = "full" }``: one test of one fact."""

    field: StrictStr
    equals: Scalar | None = None
    not_equals: Scalar | None = None
    in_: tuple[Scalar, ...] | None = Field(default=None, alias="in")
    nonempty: StrictBool | None = None

    @model_validator(mode="after")
    def _one_operator(self) -> Self:
        given = (self.equals, self.not_equals, self.in_, self.nonempty)
        if sum(v is not None for v in given) != 1:
            raise ValueError("a test needs `field` and one of equals / not_equals / in / nonempty")
        return self


@file_entry
class AllOf:
    all: tuple[Condition, ...]


@file_entry
class AnyOf:
    any: tuple[Condition, ...]


@file_entry
class Not:
    not_: Condition = Field(alias="not")


def _kind(value: Any) -> str | None:
    """Which kind of condition a TOML table is, by its key."""
    if isinstance(value, dict):
        return next((k for k in ("all", "any", "not") if k in value), "test")
    return {AllOf: "all", AnyOf: "any", Not: "not", Test: "test"}.get(type(value))


Condition = Annotated[
    Annotated[AllOf, Tag("all")]
    | Annotated[AnyOf, Tag("any")]
    | Annotated[Not, Tag("not")]
    | Annotated[Test, Tag("test")],
    Discriminator(_kind),
]


@file_entry
class Rule:
    """One ``[[rules]]`` entry. ``enabled = false`` with just an ``id`` turns a rule off."""

    id: Annotated[str, Strict(), Field(pattern=r"^[a-z0-9-]{1,64}$")]
    enabled: StrictBool = True
    severity: Annotated[Severity, BeforeValidator(_severity)] = Severity.LOW
    title: Annotated[str, Strict(), Field(max_length=120)] = ""
    reason: Annotated[str, Strict(), Field(max_length=500)] = ""  # may quote facts as {name}
    when: Condition | None = None

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.enabled and not (self.title and self.reason and self.when):
            raise ValueError(f"rule {self.id}: severity, title, reason and when are required")
        if self.when is not None and _depth(self.when) > MAX_DEPTH:
            raise ValueError(f"rule {self.id}: conditions nest more than {MAX_DEPTH} levels")
        return self


@file_entry
class RuleFile:
    rules: Annotated[tuple[Rule, ...], Field(max_length=MAX_RULES)]


# --- evaluation --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    rule_id: str
    severity: Severity
    title: str
    reason: str
    facts: dict[str, Any]  # the facts the rule looked at, for the UI


@dataclass
class RuleSet:
    rules: dict[str, Rule] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # e.g. a broken override file


def facts(
    agent: AgentInstance, caps: Capabilities | None, host_os: str | None = None
) -> dict[str, Any]:
    """What rules can test. Unknown capabilities give ``capabilities_known = false``."""
    out: dict[str, Any] = {
        "agent_type": agent.agent_type,
        "sandboxed": agent.sandboxed.value,
        "host_os": host_os or "unknown",
        "capabilities_known": caps is not None,
    }
    if caps is None:
        return out
    out.update(
        approvals=caps.approvals.value,
        approval_mode=caps.approval_mode or "",
        filesystem=caps.filesystem.value,
        network=caps.network.value,
        shell="unknown" if caps.shell is None else caps.shell,
        mcp_unpinned=[s.name for s in caps.mcp_servers if s.pinned is False],
        mcp_servers=[s.name for s in caps.mcp_servers],
        secrets=sorted({s.name for s in caps.secrets}),
        hooks=sorted({h.command for h in caps.hooks if h.command}),
        writes_anywhere=caps.filesystem is FsAccess.FULL,
    )
    return out


def matches(cond: Condition, f: dict[str, Any]) -> bool:
    match cond:
        case AllOf():
            return all(matches(c, f) for c in cond.all)
        case AnyOf():
            return any(matches(c, f) for c in cond.any)
        case Not():
            return not matches(cond.not_, f)
    value = f.get(cond.field)
    if cond.equals is not None:
        return bool(value == cond.equals)
    if cond.not_equals is not None:
        return bool(value != cond.not_equals)
    if cond.in_ is not None:
        return value in cond.in_
    return bool(value) is cond.nonempty


def _fields(cond: Condition) -> set[str]:
    """The facts a condition looks at."""
    match cond:
        case AllOf():
            return set().union(*map(_fields, cond.all))
        case AnyOf():
            return set().union(*map(_fields, cond.any))
        case Not():
            return _fields(cond.not_)
    return {cond.field}


def _depth(cond: Condition) -> int:
    match cond:
        case AllOf():
            return 1 + max(map(_depth, cond.all), default=0)
        case AnyOf():
            return 1 + max(map(_depth, cond.any), default=0)
        case Not():
            return 1 + _depth(cond.not_)
    return 1


def _render(template: str, f: dict[str, Any]) -> str:
    def value(match: re.Match[str]) -> str:
        v = f.get(match.group(1), match.group(0))
        return ", ".join(map(str, v)) if isinstance(v, list) else str(v)

    return PLACEHOLDER.sub(value, template)


def evaluate(rules: RuleSet, f: dict[str, Any]) -> list[Flag]:
    """Matching flags, most severe first."""
    flags = []
    for rule in rules.rules.values():
        if rule.when is not None and matches(rule.when, f):
            looked_at = {k: f.get(k) for k in sorted(_fields(rule.when))}
            flags.append(Flag(rule.id, rule.severity, rule.title, _render(rule.reason, f),
                              looked_at))  # fmt: skip
    return sorted(flags, key=lambda fl: (-fl.severity, fl.rule_id))


def level(flags: list[Flag], capabilities_known: bool) -> str:
    """``high`` / ``medium`` / ``low`` / ``none``, or ``unknown`` when nothing could be assessed."""
    if flags:
        return max(fl.severity for fl in flags).label
    return "none" if capabilities_known else "unknown"


# --- loading -----------------------------------------------------------------------------------


def _apply(file: RuleFile, into: RuleSet) -> None:
    for rule in file.rules:
        if rule.enabled:
            into.rules[rule.id] = rule
        else:
            into.rules.pop(rule.id, None)


def load_rules(override: Path | None = None) -> RuleSet:
    rules = RuleSet()
    parse = TypeAdapter(RuleFile).validate_python
    bundled = (resources.files("openvigie") / "risk.toml").read_text(encoding="utf-8")
    _apply(parse(tomllib.loads(bundled)), rules)
    path = override or config_dir() / "risk.toml"
    if path.exists():
        candidate = RuleSet(dict(rules.rules))
        try:
            _apply(parse(load_toml_bounded(path, max_bytes=256 * 1024)), candidate)
        except ValidationError as exc:
            _ignore(rules, path, describe(exc, "risk"))
        except (ValueError, OSError, UnsafeFileError, RecursionError) as exc:
            _ignore(rules, path, str(exc))
        else:
            rules.rules = candidate.rules
    return rules


def _ignore(rules: RuleSet, path: Path, why: str) -> None:
    rules.problems.append(f"{path} ignored: {why}")
    log.warning("risk rules: %s ignored: %s", path, why)
