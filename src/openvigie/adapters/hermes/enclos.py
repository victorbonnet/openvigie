"""Hermes in enclos sandboxes (https://github.com/victorbonnet/enclos).

Offers ``sandbox_homes``, ``sandbox_of`` and ``Arrivals``. enclos keeps each sandbox's Hermes
home at ``<data>/sandboxes/<name>/home/.hermes``, ``<data>`` being ``~/.enclos`` on macOS,
``~/.local/share/enclos`` on Linux, or ``$XDG_DATA_HOME/enclos`` / ``$ENCLOS_HOME``. The
sandboxed agent writes inside its home, so what is found there is as untrusted as the rest.

enclos's import copies an agent's Hermes databases, history included: usage from before an agent
arrived in its sandbox is counted where it came from. enclos's own ``<data>/state.db`` says when
each agent (``agents.created_at``, one per Hermes profile) and sandbox (``sandboxes.created_at``)
was made. That file is enclos's: opened read-only, and an arrival it can't give is unknown.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from openvigie.adapters.base import HostContext
from openvigie.safeio import UnsafeFileError, open_sqlite_readonly

DATA = (".enclos", ".local/share/enclos")  # macOS, Linux
SANDBOX_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")  # enclos' own rule
MAX_SANDBOXES = 100
MAX_ENTRIES = 1000
MAX_ROWS = 10_000

StatKey = tuple[tuple[int, int] | None, ...]
Table = dict[tuple[str, str | None], float]  # (sandbox, agent or None for the sandbox) → time


def sandbox_homes(ctx: HostContext) -> list[Path]:
    """``<sandbox>/home/.hermes`` of each enclos sandbox, symlinks and odd names skipped."""
    data = [home / d for home in ctx.homes for d in DATA]
    if xdg := ctx.env("XDG_DATA_HOME"):
        data.append(Path(xdg).expanduser() / "enclos")
    if own := ctx.env("ENCLOS_HOME"):
        data.append(Path(own).expanduser())
    found: list[Path] = []
    for base in dict.fromkeys(d.absolute() / "sandboxes" for d in data):
        try:
            with os.scandir(base) as it:
                entries = sorted(islice(it, MAX_ENTRIES), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            home = Path(entry.path) / "home"
            if (
                SANDBOX_NAME.match(entry.name)
                and entry.is_dir(follow_symlinks=False)
                and home.is_dir()
                and not home.is_symlink()
            ):
                found.append(home / ".hermes")
    return found[:MAX_SANDBOXES]


def sandbox_of(root: Path) -> str | None:
    """The enclos sandbox a Hermes home belongs to, if it is one's."""
    home, sandbox = root.parent, root.parent.parent
    if root.name == ".hermes" and home.name == "home" and sandbox.parent.name == "sandboxes":
        return sandbox.name if SANDBOX_NAME.match(sandbox.name) else None
    return None


class Arrivals:
    """When each Hermes profile of a sandbox arrived there, from enclos's ``state.db``; read
    again only when that file changes."""

    def __init__(self) -> None:
        self.cache: dict[Path, tuple[StatKey, Table | None]] = {}

    def of(self, root: Path, profile: str) -> float | None:
        """POSIX time the profile arrived in its sandbox; None if unknown. A profile enclos has
        no agent for arrived with its sandbox."""
        sandbox = sandbox_of(root)
        if sandbox is None:
            return None
        table = self._table(root.parents[3] / "state.db")
        if table is None:
            return None
        return table.get((sandbox, profile), table.get((sandbox, None)))

    def _table(self, db: Path) -> Table | None:
        key = (_stat(db), _stat(db.with_name(db.name + "-wal")))
        cached = self.cache.get(db)
        if cached is None or cached[0] != key:
            cached = (key, _read(db))
            self.cache[db] = cached
        return cached[1]


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read(db: Path) -> Table | None:
    try:
        conn = open_sqlite_readonly(db)
    except (OSError, UnsafeFileError, sqlite3.Error):
        return None
    try:
        sandboxes = conn.execute("select name, created_at from sandboxes limit ?", (MAX_ROWS,))
        table: Table = {}
        for name, created in sandboxes.fetchall():
            _put(table, (name, None), created)
        agents = conn.execute("select sandbox, name, created_at from agents limit ?", (MAX_ROWS,))
        for sandbox, name, created in agents.fetchall():
            if isinstance(name, str):
                _put(table, (sandbox, name), created)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return table


def _put(table: Table, key: tuple[object, str | None], created: object) -> None:
    sandbox = key[0]
    if not isinstance(sandbox, str) or not isinstance(created, str) or len(created) > 64:
        return
    try:
        when = datetime.fromisoformat(created)
    except ValueError:
        return
    if when.tzinfo is not None:
        table[(sandbox, key[1])] = when.astimezone(UTC).timestamp()
