"""What a Hermes profile can access (FR-8), from its ``config.yaml``.

Offers ``read_capabilities``. Missing settings take Hermes' defaults (``approvals.mode: smart``,
``terminal.backend: local``, ``toolsets: [hermes-cli]``). Approvals in Hermes cover dangerous
commands only: file edits never ask, so ``manual`` and ``smart`` both map to ``auto_edit``.

Messaging platforms come from the gateway (``served``); who may message the profile is set in
its ``.env`` (``TELEGRAM_ALLOWED_USERS`` and the like), which is never opened, so direct-message
reach stays unknown. Group chats count as reachable when the platform lists allowed chats or
channels. Secrets are reported by name only.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from openvigie.adapters import caps
from openvigie.adapters.common import display_path
from openvigie.model import (
    AgentInstance,
    ApprovalLevel,
    Capabilities,
    Channel,
    FsAccess,
    NetAccess,
    SecretRef,
)
from openvigie.safeio import UnsafeFileError, read_text_bounded, walk_bounded

APPROVALS = {"manual": ApprovalLevel.AUTO_EDIT, "smart": ApprovalLevel.AUTO_EDIT,
             "off": ApprovalLevel.NEVER_ASK}  # fmt: skip
CONTAINERS = {"docker", "singularity", "modal", "managed_modal", "daytona", "vercel_sandbox"}
# platform settings naming the group chats or channels a profile answers in
GROUP_KEYS = ("allowed_chats", "group_allowed_chats", "allowed_channels", "free_response_channels")
SHELL_TOOLSETS = {"terminal", "debugging"}


def read_capabilities(
    agent: AgentInstance,
    home: Path,
    homes: Iterable[Path],
    platforms: Iterable[str],
    now: datetime,
) -> Capabilities | None:
    config = home / "config.yaml"
    try:
        data = (
            yaml.safe_load(read_text_bounded(config, max_bytes=256 * 1024))
            if config.exists()
            else {}
        )
    except (yaml.YAMLError, UnicodeDecodeError, OSError, UnsafeFileError):
        return None
    data = caps.table(data)
    terminal = caps.table(data.get("terminal"))
    backend = caps.text(terminal.get("backend")) or "local"
    mode = _approval_mode(caps.table(data.get("approvals")).get("mode"))
    enabled = _enabled_toolsets(data)
    disabled = caps.texts(caps.table(data.get("agent")).get("disabled_toolsets"))
    servers, secrets = _mcp(data.get("mcp_servers"))
    filesystem, network = _reach(backend)
    return Capabilities(
        agent_id=agent.agent_id,
        observed_at=now,
        approvals=APPROVALS.get(mode, ApprovalLevel.UNKNOWN),
        approval_mode=mode,
        filesystem=filesystem,
        network=network,
        shell=_shell(enabled, disabled),
        tools_allowed=enabled,
        tools_denied=disabled,
        mcp_servers=servers,
        skills=_skills(home / "skills"),
        secrets=caps.unique([*secrets, *_config_secrets(data)]),
        sources=(display_path(config, homes),) if config.exists() else (),
        channels=tuple(_channel(p, data) for p in sorted(set(platforms))),
    )


def _approval_mode(value: Any) -> str:
    if value is False:  # YAML 1.1 reads a bare `off` as false
        return "off"
    mode = caps.text(value)
    return mode.strip().lower() if mode else "smart"


def _enabled_toolsets(data: dict[str, Any]) -> tuple[str, ...]:
    names = list(caps.texts(data.get("toolsets")) or ("hermes-cli",))
    for toolsets in caps.table(data.get("platform_toolsets")).values():
        names += caps.texts(toolsets)
    return caps.unique(names)


def _shell(enabled: tuple[str, ...], disabled: tuple[str, ...]) -> bool:
    if "terminal" in disabled:
        return False
    # "hermes-*" bundles carry the full default tool list, terminal included
    return any(t in SHELL_TOOLSETS or t.startswith("hermes-") for t in enabled)


def _reach(backend: str) -> tuple[FsAccess, NetAccess]:
    if backend == "local":
        return FsAccess.FULL, NetAccess.FULL
    if backend in CONTAINERS:
        return FsAccess.WORKSPACE, NetAccess.UNKNOWN  # egress depends on the container setup
    return FsAccess.UNKNOWN, NetAccess.UNKNOWN  # ssh: another machine's access


def _mcp(servers: Any) -> tuple[tuple[Any, ...], list[SecretRef]]:
    found, secrets = [], []
    for name, spec in list(caps.table(servers).items())[: caps.MAX_ITEMS]:
        parsed = caps.mcp_server(name, spec, "config.yaml") if isinstance(name, str) else None
        if parsed:
            found.append(parsed[0])
            secrets += parsed[1]
    return tuple(found), secrets


def _config_secrets(data: dict[str, Any]) -> list[SecretRef]:
    terminal = caps.table(data.get("terminal"))
    passed = [*caps.texts(terminal.get("env_passthrough")),
              *caps.texts(terminal.get("docker_forward_env"))]  # fmt: skip
    refs = [
        SecretRef(n, "config.yaml terminal env") for n in caps.secret_names(dict.fromkeys(passed))
    ]
    # keys written into the config itself (never their values)
    for section in ("delegation", "auxiliary", "openrouter"):
        for key, value in caps.table(data.get(section)).items():
            if (
                isinstance(key, str)
                and caps.SECRET_NAME.search(key)
                and isinstance(value, str)
                and value
            ):
                refs.append(SecretRef(f"{section}.{key}"[: caps.MAX_TEXT], "config.yaml"))
    return refs


def _skills(directory: Path) -> tuple[str, ...]:
    names = {p.parent.name for p in walk_bounded(directory, "SKILL.md", max_depth=2)}
    return tuple(sorted(names)[: caps.MAX_ITEMS])


def _channel(platform: str, data: dict[str, Any]) -> Channel:
    settings = caps.table(data.get(platform))
    groups = any(bool(caps.text(settings.get(k))) or bool(caps.texts(settings.get(k)))
                 for k in GROUP_KEYS)  # fmt: skip
    return Channel(platform=platform[: caps.MAX_TEXT], groups=True if groups else None)
