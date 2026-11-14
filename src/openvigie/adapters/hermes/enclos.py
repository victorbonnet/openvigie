"""Hermes in enclos sandboxes (https://github.com/victorbonnet/enclos).

Offers ``sandbox_homes``, ``sandbox_of``, ``inside``, ``Arrivals`` and ``Found``. enclos keeps
each sandbox's Hermes at ``<data>/sandboxes/<name>/home/.hermes``, ``<data>`` being ``~/.enclos``
on macOS, ``~/.local/share/enclos`` on Linux, or ``$XDG_DATA_HOME/enclos`` / ``$ENCLOS_HOME``.
The sandboxed agent writes inside its home, so what is found there is as untrusted as the rest,
and nothing there is read through a link that leads out of it.

An agent imported into a sandbox (``enclos agent add <sandbox>/<agent> --from``) brings its Hermes
``state.db``, history included: that usage belongs to where it came from and isn't counted again
here. enclos's ``<data>/state.db`` says when each agent it tracks (``agents.created_at``) and each
sandbox (``sandboxes.created_at``) was made; it tracks no agent for the built-in ``default``
profile, which arrives with its sandbox. That file is enclos's, opened read-only.

Known limits: enclos stamps an agent adopted from a profile made in the sandbox (``enclos agent
add`` without ``--from``) when it is adopted, so its usage from before then isn't counted; and a
sandbox removed and made again under the same name keeps the old one's agents and their cursors.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from openvigie.adapters.base import HostContext
from openvigie.adapters.common import display_path
from openvigie.safeio import UnsafeFileError, open_sqlite_readonly

DATA = (".enclos", ".local/share/enclos")  # macOS, Linux
SANDBOX_NAME = re.compile(r"^[a-z][a-z0-9-]{1,40}$")  # enclos's is_short_name
MAX_AGENT_NAME = 64
MAX_SANDBOXES = 100
MAX_ENTRIES = 1000
MAX_ROWS = 10_000
MAX_TIME = 64
DEFAULT = "default"


@dataclass(frozen=True)
class Found:
    """An agent's arrival in its sandbox (POSIX time), or why it is unknown; ``retry`` when
    enclos's state.db was busy and the next tick should try again."""

    at: float | None
    why: str = ""
    retry: bool = False


@dataclass(frozen=True)
class Created:
    """What enclos's state.db says. ``agents`` is None when that table couldn't be read."""

    sandboxes: dict[str, float]
    agents: dict[tuple[str, str], float] | None
    unusable: frozenset[tuple[str, str | None]] = field(default_factory=frozenset)
    cut: bool = False  # more agent rows than read


def sandbox_homes(
    ctx: HostContext, is_home: Callable[[Path], bool]
) -> tuple[list[Path], list[str]]:
    """The Hermes home (``is_home``) of each enclos sandbox, sorted; notes for what could not be
    listed. Sandboxes with odd names or linked folders are skipped."""
    data = [home / d for home in ctx.homes for d in DATA]
    if (xdg := ctx.env("XDG_DATA_HOME")) and Path(xdg).is_absolute():  # enclos ignores relative
        data.append(Path(xdg) / "enclos")
    if own := ctx.env("ENCLOS_HOME"):
        data.append(Path(own).expanduser())
    found: list[Path] = []
    notes: list[str] = []
    for base in dict.fromkeys(d.absolute() / "sandboxes" for d in data):
        where = display_path(base, ctx.homes)
        try:
            with os.scandir(base) as it:
                entries = list(islice(it, MAX_ENTRIES + 1))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as e:
            notes.append(f"{where} could not be listed ({e.strerror}): its sandboxes are not shown")
            continue
        if len(entries) > MAX_ENTRIES:
            notes.append(f"{where}: more entries than read, some sandboxes are not shown")
        for entry in sorted(entries[:MAX_ENTRIES], key=lambda e: e.name):
            root = Path(entry.path) / "home" / ".hermes"
            try:
                if (
                    SANDBOX_NAME.match(entry.name)
                    and entry.is_dir(follow_symlinks=False)
                    and not root.parent.is_symlink()
                    and is_home(root)
                ):
                    found.append(root)
            except OSError:
                notes.append(f"{where}/{entry.name} could not be read: not shown")
    if len(found) > MAX_SANDBOXES:
        notes.append(f"more than {MAX_SANDBOXES} enclos sandboxes run Hermes: some are not shown")
    return found[:MAX_SANDBOXES], notes


def _layout(root: Path) -> tuple[str, Path] | None:
    """(sandbox name, enclos data folder) of a Hermes home with enclos's path shape."""
    home, sandbox = root.parent, root.parent.parent
    if (
        root.name == ".hermes"
        and home.name == "home"
        and sandbox.parent.name == "sandboxes"
        and SANDBOX_NAME.match(sandbox.name)
    ):
        return sandbox.name, sandbox.parent.parent
    return None


def sandbox_of(root: Path) -> str | None:
    """The sandbox's name when a Hermes home has enclos's path shape
    (``…/sandboxes/<name>/home/.hermes``), else None."""
    layout = _layout(root)
    return layout[0] if layout else None


def inside(root: Path, path: Path) -> bool:
    """Whether ``path`` is still within the Hermes home ``root`` once links are followed, the
    home itself included: the sandboxed agent may swap any of them for a link at any time."""
    try:
        real = root.parent.resolve() / root.name
        return not real.is_symlink() and path.resolve().is_relative_to(real)
    except (OSError, RuntimeError):
        return False


class Arrivals:
    """When each Hermes profile arrived in its sandbox, from enclos's state.db; read again only
    when that file changes. A busy file isn't remembered as unreadable."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[object, object], Created | str]] = {}

    def of(self, root: Path, profile: str, now: float) -> Found:
        layout = _layout(root)
        if layout is None:
            return Found(None, "not an enclos sandbox")
        sandbox, data = layout
        read = self._read(data / "state.db")
        if isinstance(read, str):
            return Found(None, read, retry=read == BUSY)
        found = _lookup(read, sandbox, profile)
        if found.at is not None and found.at > now:
            return Found(None, "enclos's time for it is in the future")
        return found

    def _read(self, db: Path) -> Created | str:
        key = (_stat(db), _stat(db.with_name(db.name + "-wal")))
        cached = self._cache.get(db)
        if cached is not None and cached[0] == key:
            return cached[1]
        read = _read(db)
        if read != BUSY:
            self._cache[db] = (key, read)
        return read


BUSY = "enclos's state.db is busy"


def _lookup(created: Created, sandbox: str, profile: str) -> Found:
    if (sandbox, profile) in created.unusable:
        return Found(None, f"enclos's time for agent {profile} is unusable")
    if created.agents is not None and (at := created.agents.get((sandbox, profile))) is not None:
        return Found(at)
    if profile != DEFAULT and created.agents is None:
        return Found(None, "enclos's agents could not be read")
    if profile != DEFAULT and created.cut:
        return Found(None, "enclos tracks more agents than read")
    if (sandbox, None) in created.unusable:
        return Found(None, "enclos's time for the sandbox is unusable")
    if (at := created.sandboxes.get(sandbox)) is None:
        return Found(None, "enclos has no record of the sandbox")
    return Found(at)  # untracked: it came to be in the sandbox, after it


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read(db: Path) -> Created | str:
    try:
        conn = open_sqlite_readonly(db)
    except (OSError, UnsafeFileError, sqlite3.Error):
        return "enclos's state.db could not be opened"
    try:
        try:
            rows = conn.execute("select name, created_at from sandboxes limit ?", (MAX_ROWS,))
            sandbox_rows = rows.fetchall()
        except sqlite3.OperationalError as e:
            return BUSY if _busy(e) else "enclos's state.db has no readable sandboxes"
        except sqlite3.Error:
            return "enclos's state.db has no readable sandboxes"
        try:
            rows = conn.execute("select sandbox, name, created_at from agents limit ?",
                                (MAX_ROWS + 1,))  # fmt: skip
            agent_rows: list[tuple[object, ...]] | None = rows.fetchall()
        except sqlite3.OperationalError as e:
            if _busy(e):
                return BUSY
            agent_rows = None  # an enclos from before agents were tracked
        except sqlite3.Error:
            agent_rows = None
    finally:
        conn.close()
    unusable: set[tuple[str, str | None]] = set()
    sandboxes: dict[str, float] = {}
    for name, when in sandbox_rows:
        if isinstance(name, str) and SANDBOX_NAME.match(name):
            if (at := _time(when)) is None:
                unusable.add((name, None))
            else:
                sandboxes[name] = at
    agents: dict[tuple[str, str], float] | None = None
    if agent_rows is not None:
        agents = {}
        for sandbox, name, when in agent_rows[:MAX_ROWS]:
            if not (
                isinstance(sandbox, str)
                and SANDBOX_NAME.match(sandbox)
                and isinstance(name, str)
                and 0 < len(name) <= MAX_AGENT_NAME
            ):
                continue
            if (at := _time(when)) is None:
                unusable.add((sandbox, name))
            else:
                agents[(sandbox, name)] = at
    cut = agent_rows is not None and len(agent_rows) > MAX_ROWS
    return Created(sandboxes, agents, frozenset(unusable), cut)


def _busy(error: sqlite3.OperationalError) -> bool:
    return "locked" in str(error) or "busy" in str(error)


def _time(value: object) -> float | None:
    """A timezone-aware ISO time as POSIX time; anything else is unknown, never guessed."""
    if not isinstance(value, str) or len(value) > MAX_TIME:
        return None
    try:
        when = datetime.fromisoformat(value)
        return when.astimezone(UTC).timestamp() if when.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None
