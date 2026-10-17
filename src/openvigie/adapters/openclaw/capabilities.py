"""What one OpenClaw agent can access (FR-8), from ``openclaw.json``.

Offers ``read_capabilities``. An agent's settings are the root ones (``tools``,
``agents.defaults``) overridden by its ``agents.entries.<id>``.

* Commands: the ``exec`` tool runs on the gateway host unless ``sandbox.mode`` is ``all``, so
  files and network are the host's. Exec approvals set outside ``openclaw.json`` (OpenClaw keeps
  them in its state database) aren't read: without ``tools.exec`` settings, approvals are unknown.
* Channels: the accounts and group chats ``bindings`` route to the agent. DMs default to
  ``pairing``, whose approved senders live in ``credentials/`` (never opened), so how many people
  can message it is then unknown. Senders are counted from ``allowFrom`` lists, never kept; a
  ``"*"`` entry means anyone. Groups default to ``allowlist`` (only listed senders trigger it);
  ``groupPolicy: open`` lets any member do so.
* Secrets: names of credentials written into the config (bot tokens of its accounts, plugin API
  keys), never their values.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from openvigie.adapters import caps
from openvigie.adapters.common import display_path
from openvigie.model import (
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    Channel,
    FsAccess,
    Hook,
    NetAccess,
    SecretRef,
)

CONFIG = "openclaw.json"
SHELL_PROFILES = {"coding", "full"}
NO_SHELL_PROFILES = {"minimal", "messaging"}
SHELL_TOOLS = {"exec", "group:runtime", "*"}
MAX_DEPTH = 4
GROUP_POLICIES = {"allowlist", "open", "disabled"}


def read_capabilities(
    agent: AgentInstance,
    root: Path,
    name: str,
    config: dict[str, Any],
    homes: Iterable[Path],
    now: datetime,
) -> Capabilities:
    agents = caps.table(config.get("agents"))
    entry = caps.table(caps.table(agents.get("entries")).get(name))
    root_tools, own_tools = caps.table(config.get("tools")), caps.table(entry.get("tools"))
    tools = {**root_tools, **own_tools}
    exec_ = {**caps.table(caps.table(config.get("tools")).get("exec")),
             **caps.table(caps.table(entry.get("tools")).get("exec"))}  # fmt: skip
    sandbox = caps.table(entry.get("sandbox")) or caps.table(
        caps.table(agents.get("defaults")).get("sandbox")
    )
    allowed, denied = _narrowed(root_tools, own_tools)
    sandboxed = caps.text(sandbox.get("mode")) == "all"
    approvals, mode = _approvals(exec_)
    servers, mcp_secrets = _mcp(caps.table(config.get("mcp")).get("servers"))
    accounts, own, claimed = _bindings(config, name)
    return Capabilities(
        agent_id=agent.agent_id,
        observed_at=now,
        approvals=approvals,
        approval_mode=mode,
        filesystem=FsAccess.WORKSPACE if sandboxed else FsAccess.FULL,
        network=NetAccess.UNKNOWN if sandboxed else NetAccess.FULL,
        shell=_shell(caps.text(tools.get("profile")), allowed, denied, exec_),
        tools_allowed=allowed,
        tools_denied=denied,
        mcp_servers=servers,
        skills=_enabled(caps.table(config.get("skills")).get("entries")),
        hooks=tuple(Hook("internal", n) for n in _enabled(
            caps.table(caps.table(config.get("hooks")).get("internal")).get("entries"))),
        secrets=caps.unique([*mcp_secrets, *_secrets(config, accounts)])[: caps.MAX_ITEMS],
        sources=(display_path(root / CONFIG, homes),),
        channels=tuple(_channel(config, c, a, own, claimed) for c, a in accounts),
    )  # fmt: skip


def _narrowed(root: dict[str, Any], own: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Allowed and denied tools. An agent can only narrow the root policy: denials add up, and
    when both levels list allowed tools, only those in both are allowed."""
    allow_root, allow_own = caps.texts(root.get("allow")), caps.texts(own.get("allow"))
    if allow_root and allow_own:
        allowed = tuple(t for t in allow_own if t in allow_root)
    else:
        allowed = allow_own or allow_root
    denied = caps.unique([*caps.texts(root.get("deny")), *caps.texts(own.get("deny"))])
    return allowed[: caps.MAX_ITEMS], denied[: caps.MAX_ITEMS]


def _approvals(exec_: dict[str, Any]) -> tuple[ApprovalLevel, str | None]:
    security, ask = caps.text(exec_.get("security")), caps.text(exec_.get("ask"))
    if security is None and ask is None:
        return ApprovalLevel.UNKNOWN, None
    security, ask = security or "full", ask or "off"  # OpenClaw's gateway-host defaults
    mode = f"exec {security}, ask {ask}"[: caps.MAX_TEXT]
    if security == "deny" or ask == "always":
        return ApprovalLevel.ASK, mode
    if security == "allowlist":  # commands off the allowlist are asked about or refused
        return ApprovalLevel.AUTO_EDIT, mode
    return ApprovalLevel.NEVER_ASK, mode


def _shell(
    profile: str | None, allowed: tuple[str, ...], denied: tuple[str, ...], exec_: dict[str, Any]
) -> bool | None:
    if any(t in SHELL_TOOLS for t in denied) or caps.text(exec_.get("security")) == "deny":
        return False
    if profile in SHELL_PROFILES or any(t in SHELL_TOOLS for t in allowed):
        return True
    return False if profile in NO_SHELL_PROFILES else None


def _enabled(entries: Any) -> tuple[str, ...]:
    names = (k for k, v in caps.table(entries).items() if caps.table(v).get("enabled") is True)
    return tuple(sorted(n[: caps.MAX_TEXT] for n in names if isinstance(n, str)))[: caps.MAX_ITEMS]


def _mcp(servers: Any) -> tuple[tuple[Any, ...], list[SecretRef]]:
    found, secrets = [], []
    for name, spec in list(caps.table(servers).items())[: caps.MAX_ITEMS]:
        if caps.table(spec).get("enabled") is False or not isinstance(name, str):
            continue
        parsed = caps.mcp_server(name, spec, CONFIG)
        if parsed:
            found.append(parsed[0])
            secrets += parsed[1]
    return tuple(found), secrets


Groups = dict[str, dict[str, dict[str, Any]]]  # channel → group id → its settings


def _bindings(config: dict[str, Any], name: str) -> tuple[list[tuple[str, str]], Groups, Groups]:
    """The (channel, account) pairs bound to the agent, the group chats bound to it, and those
    bound to any agent (their messages go to that agent, whichever account receives them)."""
    accounts: list[tuple[str, str]] = []
    own: Groups = {}
    claimed: Groups = {}
    channels = caps.table(config.get("channels"))
    bindings = config.get("bindings")
    for binding in bindings[: caps.MAX_ITEMS] if isinstance(bindings, list) else ():
        binding = caps.table(binding)
        match = caps.table(binding.get("match"))
        channel = caps.text(match.get("channel"))
        if channel is None:
            continue
        peer = caps.table(match.get("peer"))
        if peer.get("kind") == "group" and isinstance(peer.get("id"), str):
            chats = caps.table(caps.table(channels.get(channel)).get("groups"))
            settings = caps.table(chats.get(peer["id"]))
            claimed.setdefault(channel, {})[peer["id"]] = settings
            if binding.get("agentId") == name:
                own.setdefault(channel, {})[peer["id"]] = settings
            continue
        if binding.get("agentId") != name:
            continue
        # no accountId: the channel's default account; "*": every account it has
        account = caps.text(match.get("accountId")) or "default"
        names = [account]
        if account == "*":
            configured = caps.table(caps.table(channels.get(channel)).get("accounts"))
            names = [a for a in configured if isinstance(a, str)][: caps.MAX_ITEMS] or ["default"]
        accounts += [(channel, a) for a in names if (channel, a) not in accounts]
    for channel in own:  # a group bound without an account goes through the default one
        if not any(c == channel for c, _ in accounts):
            accounts.append((channel, "default"))
    return accounts, own, claimed


def _channel(
    config: dict[str, Any], channel: str, account: str, own: Groups, claimed: Groups
) -> Channel:
    root = caps.table(caps.table(config.get("channels")).get(channel))
    settings = {**root, **caps.table(caps.table(root.get("accounts")).get(account))}
    taken = claimed.get(channel, {})
    chats = [*own.get(channel, {}).values()] + [
        caps.table(g) for k, g in caps.table(settings.get("groups")).items() if k not in taken
    ]
    dm = caps.text(settings.get("dmPolicy")) or "pairing"
    allow_from = set(caps.texts(settings.get("allowFrom")))
    group_policy = caps.text(settings.get("groupPolicy")) or "allowlist"
    chat_policies = {caps.text(g.get("groupPolicy")) or group_policy for g in chats}
    open_dms: bool | None = None
    people: set[str] | None = None  # who can instruct it; None: can't be counted
    if dm == "allowlist":
        open_dms, people = "*" in allow_from, set(allow_from)
    elif dm in ("pairing", "disabled", "open"):
        open_dms = dm == "open"
        people = set() if dm == "disabled" else None  # pairing: approvals aren't read
    groups: bool | None = None
    if group_policy == "disabled":
        groups = False
    elif chat_policies <= GROUP_POLICIES:  # an unknown policy leaves group reach unknown
        groups = "open" in chat_policies or any(
            "*" in caps.texts(g.get("allowFrom")) for g in chats
        )
        if people is not None and group_policy == "allowlist":
            people |= set(caps.texts(settings.get("groupAllowFrom")))
            for chat in chats:
                people |= set(caps.texts(chat.get("allowFrom")))
    label = channel if account == "default" else f"{channel}:{account}"
    return Channel(
        platform=label[: caps.MAX_TEXT],
        dm_policy=dm[: caps.MAX_TEXT],
        open_dms=open_dms,
        allowed_users=None if people is None or "*" in people else len(people),
        groups=groups,
    )


def _secrets(config: dict[str, Any], accounts: list[tuple[str, str]]) -> list[SecretRef]:
    refs = []
    channels = caps.table(config.get("channels"))
    for channel in dict.fromkeys(c for c, _ in accounts):  # settings all its accounts inherit
        shared = {k: v for k, v in caps.table(channels.get(channel)).items()
                  if k not in ("accounts", "groups")}  # fmt: skip
        refs += [SecretRef(f"channels.{channel}.{k}"[: caps.MAX_TEXT], CONFIG)
                 for k in caps.secret_paths(shared, 1)]  # fmt: skip
    for channel, account in accounts:
        settings = caps.table(
            caps.table(caps.table(channels.get(channel)).get("accounts")).get(account)
        )
        refs += [SecretRef(f"channels.{channel}.accounts.{account}.{k}"[: caps.MAX_TEXT], CONFIG)
                 for k in caps.secret_paths(settings, 1)]  # fmt: skip
    plugins = caps.table(caps.table(config.get("plugins")).get("entries"))
    for plugin, entry in list(plugins.items())[: caps.MAX_ITEMS]:
        if caps.table(entry).get("enabled") is not False and isinstance(plugin, str):
            refs += [
                SecretRef(f"plugins.{plugin}.{k}"[: caps.MAX_TEXT], CONFIG)
                for k in caps.secret_paths(caps.table(entry).get("config"), MAX_DEPTH)
            ]
    return refs
