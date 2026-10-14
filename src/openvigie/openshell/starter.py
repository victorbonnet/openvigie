"""A least-privilege starter OpenShell policy for an agent, from what openvigie observed (FR-11).

``generate(agent, capabilities)`` returns the policy as YAML, with a comment on every rule saying
why it is there. It only builds text: nothing is applied (that is FR-12, after the audit log).
Every value is written as a JSON string or number, which is valid YAML, so names read from agent
configs can't change the policy's structure; comments drop control characters.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from openvigie.model import AgentInstance, Capabilities, FsAccess

# A host OpenShell accepts: a name with at least two labels, or a wildcard with at least three
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"(?:\*\.(?=[^.]+\.[^.]+\.))?{_LABEL}(?:\.{_LABEL})+")  # use fullmatch
SYSTEM_READ_ONLY = ("/usr", "/lib", "/etc", "/proc", "/dev/urandom", "/var/log")
SYSTEM_READ_WRITE = ("/tmp", "/dev/null")  # noqa: S108 - paths inside the sandbox


def _host(value: str) -> str:
    if not HOST.fullmatch(value):
        raise ValueError("not a host name OpenShell accepts")
    return value


HostName = Annotated[str, AfterValidator(_host)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class _Endpoint(_Strict):
    host: HostName
    reason: Annotated[str, Field(max_length=200)]


class _Agent(_Strict):
    binaries: list[Annotated[str, Field(max_length=256)]]
    endpoints: list[_Endpoint]


class _Data(_Strict):
    agents: dict[str, _Agent]
    registries: dict[str, list[HostName]]
    registry_binaries: dict[str, list[Annotated[str, Field(max_length=256)]]]


def _data() -> _Data:
    text = (resources.files("openvigie.openshell") / "starter.toml").read_text(encoding="utf-8")
    return _Data.model_validate(tomllib.loads(text))


@dataclass(frozen=True)
class Rule:
    name: str
    reason: str
    hosts: tuple[str, ...]
    binaries: tuple[str, ...]


@dataclass(frozen=True)
class StarterPolicy:
    yaml: str
    rules: tuple[Rule, ...]
    notes: tuple[str, ...] = field(default=())  # things a person must check or add


def generate(agent: AgentInstance, caps: Capabilities | None) -> StarterPolicy:
    data = _data()
    known = data.agents.get(agent.agent_type)
    binaries = tuple(known.binaries) if known else ()
    rules: list[Rule] = []
    notes: list[str] = []
    if known:
        rules += [Rule(f"model_api_{i + 1}", e.reason, (e.host,), binaries)
                  for i, e in enumerate(known.endpoints)]  # fmt: skip
    else:
        notes.append(f"openvigie doesn't know which hosts {agent.agent_type} needs: add them.")
    if caps is not None:
        rules += _from_capabilities(caps, binaries, data, notes)
    yaml = _render(agent, caps, rules, notes)
    return StarterPolicy(yaml, tuple(rules), tuple(notes))


def _from_capabilities(caps: Capabilities, binaries: tuple[str, ...], data: _Data,
                       notes: list[str]) -> list[Rule]:  # fmt: skip
    rules: list[Rule] = []
    allowed = tuple(h for h in caps.network_allowlist if HOST.fullmatch(h.lower()))
    if allowed:
        rules.append(Rule("agent_allowlist", "hosts the agent's own settings allow it to fetch",
                          tuple(h.lower() for h in allowed), binaries))  # fmt: skip
    skipped = [h for h in caps.network_allowlist if not HOST.fullmatch(h.lower())]
    if skipped:
        notes.append(f"Left out allowlist entries that aren't host names: {', '.join(skipped)}")
    for server in caps.mcp_servers:
        slug = _slug(server.name)
        if server.url_host and HOST.fullmatch(server.url_host.lower()):
            rules.append(Rule(f"mcp_{slug}", f"MCP server {server.name} (remote)",
                              (server.url_host.lower(),), binaries))  # fmt: skip
        elif server.command in data.registries:
            reason = f"MCP server {server.name} is downloaded by {server.command} at start"
            hosts = tuple(data.registries[server.command])
            tools = tuple(data.registry_binaries.get(server.command, ()))
            rules.append(Rule(f"mcp_{slug}_packages", reason, hosts, tools))
            notes.append(f"MCP server {server.name}: add the hosts it calls itself, if any.")
        elif server.command:
            notes.append(f"MCP server {server.name} runs {server.command}: add the hosts it calls.")
    return rules


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40] or "server"


def _comment(text: str) -> str:
    return "".join(c if c.isprintable() else " " for c in text)[:200]


def _q(value: str) -> str:
    return json.dumps(value)


def _render(agent: AgentInstance, caps: Capabilities | None, rules: list[Rule],
            notes: list[str]) -> str:  # fmt: skip
    lines = [
        f"# Starter OpenShell policy for {_comment(agent.name)} ({_comment(agent.agent_type)}),",
        "# generated by openvigie from what it observed. Review it before use:",
        "#   openshell sandbox create --policy starter.yaml -- <agent command>",
        *(f"# NOTE: {_comment(n)}" for n in notes),
        "version: 1",
        "filesystem_policy:",
    ]
    read_only = caps is not None and caps.filesystem is FsAccess.READ_ONLY
    lines.append(f"  include_workdir: {'false' if read_only else 'true'}"
                 + ("  # the agent only reads files" if read_only
                    else "  # the project, read-write"))  # fmt: skip
    lines += ["  read_only:", *(f"    - {_q(p)}" for p in SYSTEM_READ_ONLY)]
    lines += ["  read_write:", *(f"    - {_q(p)}" for p in SYSTEM_READ_WRITE)]
    for root in caps.writable_roots if caps else ():
        lines.append(
            f"  # the agent may also write {_comment(root)} on the host: mount it and add it"
        )
    lines += ["landlock:", '  compatibility: "best_effort"']
    if not rules:
        lines.append("network_policies: {}  # no network access at all")
        return "\n".join(lines) + "\n"
    lines.append("network_policies:")
    for rule in rules:
        lines += [f"  # {_comment(rule.reason)}", f"  {rule.name}:", "    endpoints:"]
        for host in rule.hosts:
            lines += [f"      - host: {_q(host)}", "        port: 443"]
        lines.append("    binaries:" if rule.binaries else "    binaries: []  # add the binaries")
        lines += [f"      - path: {_q(b)}" for b in rule.binaries]
    return "\n".join(lines) + "\n"
