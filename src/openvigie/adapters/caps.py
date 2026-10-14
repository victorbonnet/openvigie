"""Helpers shared by adapters that report capabilities (FR-8).

Everything here works on untrusted config values: strings are bounded, commands are reduced
to their first word, and env values are never returned, only their names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

from openvigie.model import McpServer, SecretRef
from openvigie.redact import is_sensitive_key

MAX_ITEMS = 200
MAX_TEXT = 256
PACKAGE_RUNNERS = {"npx", "bunx", "pnpx", "uvx", "pipx", "yarn", "pnpm", "dlx"}
SECRET_NAME = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|credential|private[_-]?key|_key$|^key$|auth)"
)


def table(value: Any) -> dict[str, Any]:
    """``value`` if it is a table (dict), else an empty one: config files are untrusted."""
    return value if isinstance(value, dict) else {}


def text(value: Any) -> str | None:
    return value[:MAX_TEXT] if isinstance(value, str) and value else None


def texts(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(t for v in values[:MAX_ITEMS] if (t := text(v)))


def first_word(command: Any) -> str | None:
    """``/usr/local/bin/jq -r .x`` → ``jq``: enough to recognise a tool, never its arguments."""
    if not isinstance(command, str) or not command.strip():
        return None
    return PurePath(command.strip().split()[0]).name[:MAX_TEXT] or None


def secret_names(env: Any) -> tuple[str, ...]:
    """Names of env entries that look like secrets. Values are never looked at."""
    if not isinstance(env, dict):
        return ()
    names = [k for k in list(env)[:MAX_ITEMS] if isinstance(k, str) and len(k) <= MAX_TEXT]
    return tuple(sorted(n for n in names if is_sensitive_key(n) or SECRET_NAME.search(n)))


def pinned(command: str | None, args: Any) -> bool | None:
    """Whether a package-runner command pins the package version; ``None`` if not a runner."""
    if command not in PACKAGE_RUNNERS:
        return None
    words = [a for a in args[:20] if isinstance(a, str)] if isinstance(args, list) else []
    package = next((w for w in words if not w.startswith("-") and w not in ("dlx", "exec")), None)
    if package is None:
        return False
    if "==" in package:  # uvx / pipx: pkg==1.2.3
        return True
    name = package[1:] if package.startswith("@") else package  # @scope/pkg@1.2.3
    version = name.partition("@")[2]
    return bool(version) and version != "latest"


def mcp_server(name: str, spec: Any, where: str) -> tuple[McpServer, list[SecretRef]] | None:
    """One ``mcpServers`` entry (Claude Code JSON or Codex TOML shape)."""
    if not isinstance(spec, dict) or not text(name):
        return None
    url = text(spec.get("url"))
    command = first_word(spec.get("command"))
    transport = text(spec.get("type")) or text(spec.get("transport"))
    transport = transport or ("http" if url else "stdio")
    host = (urlsplit(url).hostname or None) if url else None
    secrets = secret_names(spec.get("env"))
    headers = secret_names(spec.get("headers") or spec.get("http_headers"))
    server = McpServer(name=name[:MAX_TEXT], transport=transport, command=command, url_host=host,
                       pinned=pinned(command, spec.get("args")),
                       secret_names=secrets + headers)  # fmt: skip
    refs = [SecretRef(s, f"{where} mcp:{name}"[:MAX_TEXT]) for s in secrets + headers]
    return server, refs


def unique[T](items: Iterable[T]) -> tuple[T, ...]:
    return tuple(dict.fromkeys(items))
