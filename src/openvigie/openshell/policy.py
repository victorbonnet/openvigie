"""Network rules an admin writes for a sandbox (FR-12, PRD D15): parse, check, show, compare.

Offers ``NetworkPolicies`` (the rules, keyed by rule name), ``parse_yaml`` (editor text → rules,
or ``PolicyError``), ``from_json`` / ``to_json`` (the exact form carried on the wire and sent to
OpenShell), ``to_yaml`` (rules → editor text), ``diff`` (rule-by-rule changes for review) and
``allowing`` (rules plus one that allows a blocked request, FR-18).

This is the editor's boundary model (D12): strict, bounded, and unknown fields are rejected, so
an admin can only write what openvigie understands. YAML is read with ``safe_load`` through a
loader that refuses aliases and anchors (no expansion bombs), from at most 64 KiB of text.
Errors name a line or a field, never the text itself. Only ``network_policies`` is taken from
the text: OpenShell applies filesystem, process and landlock sections to new sandboxes only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

MAX_TEXT = 64 * 1024
RuleKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
Short = Annotated[str, Field(min_length=1, max_length=256)]
Port = Annotated[int, Field(ge=1, le=65535)]
# sections of a full policy that the editor accepts in pasted text but doesn't apply
IGNORED_SECTIONS = ("version", "filesystem_policy", "landlock", "process")


class PolicyError(ValueError):
    """Text that isn't a valid set of network rules; the message never quotes the text."""


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)


class Endpoint(_Model):
    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9*][A-Za-z0-9*.-]{0,252}$")] | None = None
    port: Port | None = None
    ports: Annotated[list[Port], Field(max_length=64)] = []
    path: Annotated[str, Field(max_length=1024)] | None = None
    protocol: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")] | None = None
    access: Literal["read-only", "read-write", "full"] | None = None
    enforcement: Literal["enforce", "audit"] | None = None
    allowed_ips: Annotated[list[Short], Field(max_length=64)] = []

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.host is None and not self.allowed_ips:
            raise ValueError("an endpoint needs a host or allowed_ips")
        if self.port is None and not self.ports:
            raise ValueError("an endpoint needs a port or ports")
        return self


class Binary(_Model):
    path: Annotated[str, Field(pattern=r"^/", max_length=1024)]


class Rule(_Model):
    name: RuleKey | None = None
    endpoints: Annotated[list[Endpoint], Field(max_length=64)] = []
    binaries: Annotated[list[Binary], Field(max_length=64)] = []


NetworkPolicies = Annotated[dict[RuleKey, Rule], Field(max_length=128)]


class _Document(_Model):
    """What the editor accepts: ``network_policies``, plus full-policy sections it ignores."""

    model_config = ConfigDict(extra="allow")  # checked below: only IGNORED_SECTIONS

    network_policies: NetworkPolicies | None = None

    @model_validator(mode="after")
    def _known(self) -> Self:
        unknown = sorted(set(self.model_extra or {}) - set(IGNORED_SECTIONS))
        if unknown:
            raise ValueError(f"unknown section {unknown[0][:64]!r}")
        return self


_RULES: TypeAdapter[dict[str, Rule]] = TypeAdapter(NetworkPolicies)


@dataclass(frozen=True)
class Parsed:
    rules: dict[str, Rule]
    ignored: tuple[str, ...]  # full-policy sections present in the text, not applied


def _load(text: str) -> Any:
    import yaml  # only the hub parses YAML; the collector needs JSON only

    class NoAliases(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            event = self.peek_event()  # type: ignore[no-untyped-call]
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise PolicyError(f"line {event.start_mark.line + 1}: YAML anchors and aliases "
                                  "are not allowed")  # fmt: skip
            return super().compose_node(parent, index)

    return yaml.load(text, Loader=NoAliases)  # noqa: S506 - a SafeLoader subclass


def parse_yaml(text: str) -> Parsed:
    """Editor text → rules. Raises ``PolicyError``."""
    import yaml

    if len(text) > MAX_TEXT:
        raise PolicyError(f"the text is over {MAX_TEXT // 1024} KiB")
    try:
        data = _load(text)
    except yaml.MarkedYAMLError as exc:
        where = f"line {exc.problem_mark.line + 1}: " if exc.problem_mark else ""
        raise PolicyError(f"{where}not valid YAML ({exc.problem or 'syntax error'})") from None
    except (yaml.YAMLError, RecursionError):
        raise PolicyError("not valid YAML") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise PolicyError("expected a mapping with a network_policies section")
    if "network_policies" not in data:
        raise PolicyError("no network_policies section (write `network_policies: {}` for none)")
    try:
        document = _Document.model_validate(data)
    except ValidationError as exc:
        raise PolicyError(_describe(exc)) from None
    rules = _named(document.network_policies or {})
    return Parsed(rules, tuple(k for k in IGNORED_SECTIONS if k in data))


def from_json(text: str) -> dict[str, Rule]:
    """The wire form → rules. Raises ``PolicyError``."""
    try:
        return _named(_RULES.validate_json(text))
    except ValidationError as exc:
        raise PolicyError(_describe(exc)) from None


def to_json(rules: dict[str, Rule]) -> str:
    """Canonical JSON (sorted, compact, no empty fields): the wire form and OpenShell's input."""
    plain = {k: r.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
             for k, r in rules.items()}  # fmt: skip
    return json.dumps(plain, sort_keys=True, separators=(",", ":"))


def allowing(rules: dict[str, Rule], binary: str, host: str, port: int) -> dict[str, Rule]:
    """``rules`` plus one rule letting ``binary`` reach ``host:port`` (FR-18 → FR-12): scoped to
    the program that was blocked. ``host`` is literal: a glob in it (the blocked process chose it)
    is refused rather than turned into a wildcard rule."""
    if any(c in host for c in "*?[]"):
        raise PolicyError("a blocked destination with a wildcard can't be allowed from here")
    base = "allow_" + (re.sub(r"[^A-Za-z0-9]+", "_", host).strip("_")[:48] or "host") + f"_{port}"
    name, n = base, 1
    while name in rules:
        n += 1
        name = f"{base}_{n}"
    try:
        rule = Rule(name=name, endpoints=[Endpoint(host=host, port=port)],
                    binaries=[Binary(path=binary)])  # fmt: skip
    except ValidationError as exc:
        raise PolicyError(_describe(exc)) from None
    return {**rules, name: rule}


def to_yaml(rules: dict[str, Rule]) -> str:
    """Editor text. Every value is a JSON scalar, which is valid YAML (as in the starter)."""
    if not rules:
        return "network_policies: {}\n"
    lines = ["network_policies:"]
    for key in sorted(rules):
        rule = rules[key]
        lines.append(f"  {key}:")
        lines.append("    endpoints:" if rule.endpoints else "    endpoints: []")
        for e in rule.endpoints:
            fields = e.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            for i, (name, value) in enumerate(fields.items()):
                lines.append(f"      {'- ' if i == 0 else '  '}{name}: {json.dumps(value)}")
        lines.append("    binaries:" if rule.binaries else "    binaries: []")
        lines += [f"      - path: {json.dumps(b.path)}" for b in rule.binaries]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Change:
    rule: str
    change: Literal["added", "removed", "changed"]
    details: tuple[str, ...]  # one line per endpoint or binary that differs


def diff(old: dict[str, Rule], new: dict[str, Rule]) -> list[Change]:
    """Rule-by-rule changes, in rule order."""
    out = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        if before is None or after is None:
            rule = after or before
            assert rule is not None  # noqa: S101 - one of them is set
            out.append(Change(key, "added" if before is None else "removed", _lines(rule)))
            continue
        was, now = set(_lines(before)), set(_lines(after))
        details = tuple([f"- {line}" for line in _lines(before) if line not in now]
                        + [f"+ {line}" for line in _lines(after) if line not in was])  # fmt: skip
        out.append(Change(key, "changed", details))
    return out


def _lines(rule: Rule) -> tuple[str, ...]:
    return tuple([_endpoint(e) for e in rule.endpoints]
                 + [f"binary {b.path}" for b in rule.binaries])  # fmt: skip


def _endpoint(e: Endpoint) -> str:
    ports = "/".join(map(str, e.ports or ((e.port,) if e.port else ())))
    target = e.host or ", ".join(e.allowed_ips)
    extra = [x for x in (e.protocol, e.access, e.path and f"path {e.path}") if x]
    if e.allowed_ips and e.host:
        extra.append(f"ips {', '.join(e.allowed_ips)}")
    enforcement = "" if e.enforcement == "enforce" else " (audit only)"
    return f"{target}:{ports}{' ' + ' '.join(extra) if extra else ''}{enforcement}"


def _named(rules: dict[str, Rule]) -> dict[str, Rule]:
    """OpenShell repeats the key as the rule's ``name``; fill it in, refuse a mismatch."""
    out = {}
    for key, rule in rules.items():
        if rule.name not in (None, key):
            raise PolicyError(f"network_policies.{key}: name must match the key")
        out[key] = rule.model_copy(update={"name": key})
    return out


def _describe(exc: ValidationError) -> str:
    first = exc.errors(include_input=False, include_url=False, include_context=False)[0]
    where = ".".join(str(p) for p in first["loc"]) or "policy"
    message = {"extra_forbidden": "not a field openvigie's editor supports",
               "missing": "missing"}.get(first["type"], first["msg"])  # fmt: skip
    return f"{where}: {message.removeprefix('Value error, ')}"
