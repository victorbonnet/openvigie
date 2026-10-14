"""Claude Code: what an install can access (FR-8), from its own config files, read-only.

Sources: ``settings.json`` / ``settings.local.json`` in the config folder (permissions, sandbox,
hooks, env), ``~/.claude.json`` (MCP servers, user-wide and per project), ``.mcp.json`` of known
projects inside the watched homes, and ``skills/*/``. ``~/.claude.json`` also holds account data:
only its ``mcpServers`` entries are kept, the rest is dropped as soon as it is parsed.
Credentials (``.credentials.json``) are never opened; env values are never kept, only names.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from openvigie.adapters.base import ProcessInfo
from openvigie.adapters.caps import (
    MAX_ITEMS,
    first_word,
    mcp_server,
    secret_names,
    text,
    texts,
    unique,
)
from openvigie.adapters.common import display_path
from openvigie.model import (
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    FsAccess,
    Hook,
    McpServer,
    NetAccess,
    SecretRef,
)
from openvigie.safeio import UnsafeFileError, load_json_bounded, resolve_within

SETTINGS_MAX = 1024 * 1024
STATE_MAX = 16 * 1024 * 1024  # ~/.claude.json grows with history
MAX_PROJECTS = 50
CACHE_TTL = timedelta(hours=1)
MODES = {
    "default": ApprovalLevel.ASK,
    "plan": ApprovalLevel.ASK,
    "acceptEdits": ApprovalLevel.AUTO_EDIT,
    "bypassPermissions": ApprovalLevel.NEVER_ASK,
}
BYPASS_FLAGS = ("--dangerously-skip-permissions",)


def _load(path: Path, limit: int, read: list[Path]) -> dict[str, Any]:
    try:
        data = load_json_bounded(path, limit)
    except (OSError, UnsafeFileError, ValueError):
        return {}
    read.append(path)
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _merge(*settings: dict[str, Any]) -> dict[str, Any]:
    """Later files win per key, permissions lists are combined (as Claude Code does)."""
    merged: dict[str, Any] = {}
    perms: dict[str, Any] = {}
    for one in settings:
        merged.update(one)
        p = one.get("permissions")
        if isinstance(p, dict):
            for key, value in p.items():
                if isinstance(value, list) and isinstance(perms.get(key), list):
                    perms[key] = perms[key] + value
                else:
                    perms[key] = value
    merged["permissions"] = perms
    return merged


def mode_from_cmdline(procs: list[ProcessInfo]) -> str | None:
    for proc in procs:
        args = list(proc.cmdline)
        if any(flag in args for flag in BYPASS_FLAGS):
            return "bypassPermissions"
        if "--permission-mode" in args:
            i = args.index("--permission-mode")
            if i + 1 < len(args):
                return args[i + 1][:64]
    return None


def _hooks(raw: Any) -> list[Hook]:
    out: list[Hook] = []
    if not isinstance(raw, dict):
        return out
    for event, groups in list(raw.items())[:MAX_ITEMS]:
        for group in groups[:MAX_ITEMS] if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            for hook in inner[:MAX_ITEMS] if isinstance(inner, list) else []:
                if isinstance(hook, dict) and isinstance(event, str):
                    out.append(Hook(event[:64], first_word(hook.get("command"))))
    return out


def read_capabilities(
    agent: AgentInstance,
    root: Path,
    homes: tuple[Path, ...],
    procs: list[ProcessInfo],
    now: Any,
) -> Capabilities:
    read: list[Path] = []
    settings = _merge(
        _load(root / "settings.json", SETTINGS_MAX, read),
        _load(root / "settings.local.json", SETTINGS_MAX, read),
    )
    perms = settings["permissions"]
    mode = mode_from_cmdline(procs) or text(perms.get("defaultMode")) or "default"
    allow, deny = texts(perms.get("allow")), texts(perms.get("deny"))
    sandbox = _dict(settings.get("sandbox"))
    sandboxed = sandbox.get("enabled") is True
    network = _dict(sandbox.get("network"))
    domains = texts(network.get("allowedDomains"))

    servers: list[McpServer] = []
    secrets = [SecretRef(n, "settings.json env") for n in secret_names(settings.get("env"))]
    state_file = root.parent / ".claude.json" if root.name == ".claude" else root / ".claude.json"
    state = _load(state_file, STATE_MAX, read)
    entries: list[tuple[str, Any, str]] = []
    user_servers = state.get("mcpServers")
    if isinstance(user_servers, dict):
        entries += [(n, s, "user") for n, s in user_servers.items()]
    projects = _dict(state.get("projects"))
    del state  # account data: not kept
    for project, conf in list(projects.items())[:MAX_PROJECTS]:
        if isinstance(conf, dict) and isinstance(conf.get("mcpServers"), dict):
            entries += [(n, s, "project") for n, s in conf["mcpServers"].items()]
        mcp_json = _project_file(project, homes)
        if mcp_json is not None:
            shared = _load(mcp_json, SETTINGS_MAX, read).get("mcpServers")
            if isinstance(shared, dict):
                entries += [(n, s, ".mcp.json") for n, s in shared.items()]
    for name, spec, where in entries[:MAX_ITEMS]:
        found = mcp_server(name, spec, where) if isinstance(name, str) else None
        if found:
            servers.append(found[0])
            secrets += found[1]

    skills = _skills(root / "skills")

    return Capabilities(
        agent_id=agent.agent_id,
        observed_at=now,
        approvals=MODES.get(mode, ApprovalLevel.UNKNOWN),
        approval_mode=mode,
        filesystem=FsAccess.WORKSPACE if sandboxed else FsAccess.FULL,
        writable_roots=texts(perms.get("additionalDirectories")),
        network=(NetAccess.ALLOWLIST if domains else NetAccess.UNKNOWN)
        if sandboxed
        else NetAccess.FULL,
        network_allowlist=domains,
        shell=not any(d == "Bash" or d.startswith("Bash(*") for d in deny),
        tools_allowed=allow,
        tools_denied=deny,
        mcp_servers=unique(servers),
        skills=tuple(skills),
        hooks=unique(_hooks(settings.get("hooks"))),
        secrets=unique(secrets),
        sources=tuple(display_path(p, homes) for p in read),
    )


def _project_file(project: Any, homes: tuple[Path, ...]) -> Path | None:
    """``<project>/.mcp.json``, only for projects inside a watched home (paths are untrusted)."""
    if not isinstance(project, str) or len(project) > 4096:
        return None
    for home in homes:
        try:
            relative = Path(project).relative_to(home)
            return resolve_within(home, relative / ".mcp.json")
        except (ValueError, UnsafeFileError, OSError):
            continue
    return None


def _skills(folder: Path) -> list[str]:
    """Names of skill folders (each holding a SKILL.md); symlinks are not followed."""
    if not folder.is_dir() or folder.is_symlink():
        return []
    found = [p.name for p in folder.iterdir()
             if p.is_dir() and not p.is_symlink() and (p / "SKILL.md").is_file()]  # fmt: skip
    return sorted(found)[:MAX_ITEMS]


def file_states(root: Path) -> tuple[tuple[str, int, int], ...]:
    """(path, mtime, size) of the files capabilities come from, to skip unchanged re-reads.

    Project ``.mcp.json`` files aren't part of the key; the adapter re-reads everything at
    least every ``CACHE_TTL`` anyway, so their edits show up within that time.
    """
    state_file = root.parent / ".claude.json" if root.name == ".claude" else root / ".claude.json"
    out = []
    for path in (root / "settings.json", root / "settings.local.json", state_file, root / "skills"):
        try:
            st = path.lstat()
        except OSError:
            continue
        out.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(out)
