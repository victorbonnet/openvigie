"""Helpers shared by built-in adapters."""

from __future__ import annotations

import pwd
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from openvigie.adapters.base import HostContext, ProcessInfo
from openvigie.safeio import walk_bounded


def candidate_roots(ctx: HostContext, dirname: str, env_var: str | None) -> list[Path]:
    """``<home>/<dirname>`` for each configured home, plus ``$env_var`` if set; deduplicated."""
    roots: list[Path] = [home / dirname for home in ctx.homes]
    if env_var and (override := ctx.env(env_var)):
        roots.append(Path(override).expanduser())
    seen: set[Path] = set()
    unique = []
    for root in roots:
        key = root.expanduser().absolute()
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def display_path(path: Path, homes: Iterable[Path]) -> str:
    """Home-relative path for display (``~/.claude``), never leaking the user's home dir."""
    for home in homes:
        try:
            return "~/" + str(path.relative_to(home))
        except ValueError:
            continue
    return str(path)


def owner_name(path: Path) -> str | None:
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (OSError, KeyError):
        return None


def newest_mtime(root: Path, pattern: str, *, max_depth: int = 4) -> tuple[datetime, Path] | None:
    """Most recently modified file matching ``pattern`` under ``root`` (stat only)."""
    best: tuple[float, Path] | None = None
    for path in walk_bounded(root, pattern, max_depth=max_depth):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path)
    if best is None:
        return None
    return datetime.fromtimestamp(best[0], UTC), best[1]


def attribute_processes(
    processes: Iterable[ProcessInfo], root: Path, roots: list[Path]
) -> list[ProcessInfo]:
    """Processes that belong to ``root``: all of them if it's the only root, else same owner."""
    procs = list(processes)
    if len(roots) <= 1:
        return procs
    owner = owner_name(root)
    return [p for p in procs if owner is not None and p.username == owner]
