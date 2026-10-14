"""Rule-based risk flags (FR-9, PRD D10): no numeric score, every flag explained.

Rules are data (``risk.toml`` shipped with the package, plus ``<config dir>/risk.toml`` to add
or replace rules by id). A rule matches a small set of *facts* about an agent through a tiny
condition language: ``{field, equals}``, ``{field, in}``, ``{field, not_equals}``,
``{field, nonempty}``, and ``all`` / ``any`` / ``not`` over those. Nothing in a rule file is ever
evaluated as code. A broken override file is reported and ignored: the bundled rules stay.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from enum import IntEnum
from importlib import resources
from pathlib import Path
from typing import Any

from openvigie.config import config_dir
from openvigie.model import AgentInstance, Capabilities, FsAccess
from openvigie.safeio import UnsafeFileError, load_toml_bounded

log = logging.getLogger(__name__)

MAX_RULES = 200
MAX_DEPTH = 8
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
OPERATORS = {"equals", "not_equals", "in", "nonempty"}


class Severity(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Rule:
    rule_id: str
    severity: Severity
    title: str
    reason: str
    when: dict[str, Any]


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


def _check(cond: Any, f: dict[str, Any], depth: int = 0) -> bool:
    if not isinstance(cond, dict) or depth > MAX_DEPTH:
        raise ValueError("a condition must be a table")
    if "all" in cond:
        return all(_check(c, f, depth + 1) for c in _list(cond["all"]))
    if "any" in cond:
        return any(_check(c, f, depth + 1) for c in _list(cond["any"]))
    if "not" in cond:
        return not _check(cond["not"], f, depth + 1)
    name = cond.get("field")
    ops = OPERATORS & set(cond)
    if not isinstance(name, str) or len(ops) != 1:
        raise ValueError("a test needs `field` and one of equals / not_equals / in / nonempty")
    value = f.get(name)
    (op,) = ops
    if op == "equals":
        return bool(value == cond["equals"])
    if op == "not_equals":
        return bool(value != cond["not_equals"])
    if op == "in":
        return value in _list(cond["in"])
    return bool(value) is bool(cond["nonempty"])


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected a list")
    return value


def _fields(cond: Any) -> set[str]:
    if not isinstance(cond, dict):
        return set()
    found = {cond["field"]} if isinstance(cond.get("field"), str) else set()
    for key in ("all", "any"):
        for c in cond.get(key, []) if isinstance(cond.get(key), list) else []:
            found |= _fields(c)
    return found | _fields(cond.get("not"))


def _render(template: str, f: dict[str, Any]) -> str:
    def value(match: re.Match[str]) -> str:
        v = f.get(match.group(1), match.group(0))
        return ", ".join(map(str, v)) if isinstance(v, list) else str(v)

    return PLACEHOLDER.sub(value, template)


def evaluate(rules: RuleSet, f: dict[str, Any]) -> list[Flag]:
    """Matching flags, most severe first."""
    flags = []
    for rule in rules.rules.values():
        try:
            matched = _check(rule.when, f)
        except (ValueError, KeyError, TypeError):
            continue  # validated at load time; never let one rule break the page
        if matched:
            looked_at = {k: f.get(k) for k in sorted(_fields(rule.when))}
            flags.append(Flag(rule.rule_id, rule.severity, rule.title, _render(rule.reason, f),
                              looked_at))  # fmt: skip
    return sorted(flags, key=lambda fl: (-fl.severity, fl.rule_id))


def level(flags: list[Flag], capabilities_known: bool) -> str:
    """``high`` / ``medium`` / ``low`` / ``none``, or ``unknown`` when nothing could be assessed."""
    if flags:
        return max(fl.severity for fl in flags).label
    return "none" if capabilities_known else "unknown"


def _parse(data: dict[str, Any], into: RuleSet, where: str) -> None:
    rules = data.get("rules")
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise ValueError(f"{where}: expected [[rules]] (at most {MAX_RULES})")
    for raw in rules:
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: each rule must be a table")
        rule_id, severity = raw.get("id"), raw.get("severity")
        if not isinstance(rule_id, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", rule_id):
            raise ValueError(f"{where}: rule id must be lowercase letters, digits and '-'")
        if raw.get("enabled") is False:
            into.rules.pop(rule_id, None)
            continue
        if severity not in ("low", "medium", "high"):
            raise ValueError(f"{where}: rule {rule_id}: severity must be low, medium or high")
        title, reason, when = raw.get("title"), raw.get("reason"), raw.get("when")
        if not isinstance(title, str) or not isinstance(reason, str):
            raise ValueError(f"{where}: rule {rule_id}: title and reason are required")
        if not isinstance(when, dict):
            raise ValueError(f"{where}: rule {rule_id}: `when` must be a table")
        _check(when, {})  # syntax check against empty facts
        into.rules[rule_id] = Rule(rule_id, Severity[severity.upper()], title[:120], reason[:500],
                                   when)  # fmt: skip


def load_rules(override: Path | None = None) -> RuleSet:
    rules = RuleSet()
    bundled = (resources.files("openvigie") / "risk.toml").read_text(encoding="utf-8")
    _parse(tomllib.loads(bundled), rules, "bundled risk.toml")
    path = override or config_dir() / "risk.toml"
    if path.exists():
        candidate = RuleSet(dict(rules.rules))
        try:
            _parse(load_toml_bounded(path, max_bytes=256 * 1024), candidate, str(path))
        except (ValueError, OSError, UnsafeFileError) as exc:
            rules.problems.append(f"{path} ignored: {exc}")
            log.warning("risk rules: %s ignored: %s", path, exc)
        else:
            rules.rules = candidate.rules
    return rules
