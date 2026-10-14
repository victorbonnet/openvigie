"""Codex CLI: what an install can access (FR-8), from ``config.toml``, read-only.

The active profile (``profile = "…"`` → ``[profiles.<name>]``) overrides top-level keys, and
flags on the running process (``--sandbox``, ``--ask-for-approval``, ``--full-auto``,
``--dangerously-bypass-approvals-and-sandbox``) override both. ``auth.json`` is never opened;
env values are never kept, only names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openvigie.adapters.base import ProcessInfo
from openvigie.adapters.caps import MAX_ITEMS, mcp_server, secret_names, text, texts, unique
from openvigie.adapters.common import display_path
from openvigie.model import (
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    FsAccess,
    McpServer,
    NetAccess,
    SecretRef,
)
from openvigie.safeio import UnsafeFileError, load_toml_bounded

CONFIG_MAX = 256 * 1024
APPROVALS = {
    "untrusted": ApprovalLevel.ASK,
    "on-failure": ApprovalLevel.AUTO_EDIT,
    "on-request": ApprovalLevel.AUTO_EDIT,
    "never": ApprovalLevel.NEVER_ASK,
}
SANDBOX = {
    "read-only": FsAccess.READ_ONLY,
    "workspace-write": FsAccess.WORKSPACE,
    "danger-full-access": FsAccess.FULL,
}
HAS_SHELL = True  # Codex always has a shell tool; its sandbox decides what the shell can reach
BYPASS = ("--dangerously-bypass-approvals-and-sandbox", "--yolo")


def flags(procs: list[ProcessInfo]) -> tuple[str | None, str | None]:
    """(sandbox mode, approval policy) set on the command line of a running Codex, if any."""
    for proc in procs:
        args = list(proc.cmdline)
        if any(b in args for b in BYPASS):
            return "danger-full-access", "never"
        sandbox = _value(args, "--sandbox", "-s")
        approval = _value(args, "--ask-for-approval", "-a")
        if "--full-auto" in args:
            sandbox, approval = sandbox or "workspace-write", approval or "on-request"
        if sandbox or approval:
            return sandbox, approval
    return None, None


def _value(args: list[str], *names: str) -> str | None:
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1][:64]
        for name in names:
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1][:64]
    return None


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_capabilities(
    agent: AgentInstance, root: Path, homes: tuple[Path, ...], procs: list[ProcessInfo], now: Any
) -> Capabilities:
    config_path = root / "config.toml"
    sources: list[str] = []
    try:
        config = load_toml_bounded(config_path, CONFIG_MAX)
        sources.append(display_path(config_path, homes))
    except (OSError, UnsafeFileError, ValueError):
        config = {}
    profile = text(config.get("profile"))
    active = (
        {**config, **_table(_table(config.get("profiles")).get(profile))} if profile else config
    )
    cli_sandbox, cli_approval = flags(procs)
    sandbox = cli_sandbox or text(active.get("sandbox_mode"))
    approval = cli_approval or text(active.get("approval_policy"))
    workspace = _table(active.get("sandbox_workspace_write"))
    filesystem = SANDBOX.get(sandbox or "", FsAccess.UNKNOWN)
    if filesystem is FsAccess.FULL:
        network = NetAccess.FULL
    elif filesystem is FsAccess.WORKSPACE:
        network = NetAccess.FULL if workspace.get("network_access") is True else NetAccess.NONE
    elif filesystem is FsAccess.READ_ONLY:
        network = NetAccess.NONE
    else:
        network = NetAccess.UNKNOWN

    servers: list[McpServer] = []
    secrets: list[SecretRef] = []
    for name, spec in list(_table(active.get("mcp_servers")).items())[:MAX_ITEMS]:
        found = mcp_server(name, spec, "config.toml") if isinstance(name, str) else None
        if found:
            servers.append(found[0])
            secrets += found[1]
        bearer = _table(spec).get("bearer_token_env_var")
        if isinstance(bearer, str) and bearer:
            secrets.append(SecretRef(bearer[:256], f"config.toml mcp:{name}"[:256]))
    policy = _table(active.get("shell_environment_policy"))
    secrets += [
        SecretRef(n, "shell_environment_policy.set") for n in secret_names(policy.get("set"))
    ]

    return Capabilities(
        agent_id=agent.agent_id,
        observed_at=now,
        approvals=APPROVALS.get(approval or "", ApprovalLevel.UNKNOWN),
        approval_mode=approval,
        filesystem=filesystem,
        writable_roots=texts(workspace.get("writable_roots")),
        network=network,
        shell=HAS_SHELL,
        mcp_servers=unique(servers),
        secrets=unique(secrets),
        sources=tuple(sources),
    )
