"""Bounded, defensive readers for files that agents write (configs, logs, transcripts).

Adapters never call ``open()`` on agent files directly. Everything here caps sizes, refuses
non-regular files (FIFOs, devices), does not follow symlinks, survives malformed data and
counts what it skipped so adapters can report health.
"""

from __future__ import annotations

import errno
import fnmatch
import json
import os
import sqlite3
import stat
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

MiB = 1024 * 1024
DEFAULT_MAX_FILE = 4 * MiB
DEFAULT_MAX_LINE = 1 * MiB
DEFAULT_TAIL_BUDGET = 8 * MiB


class UnsafeFileError(OSError):
    """The path is not a regular file, is a symlink, is too large or escapes its root."""


@dataclass
class ReadStats:
    lines: int = 0
    parse_errors: int = 0
    oversized: int = 0

    def add(self, other: ReadStats) -> None:
        self.lines += other.lines
        self.parse_errors += other.parse_errors
        self.oversized += other.oversized


def open_regular(path: Path) -> int:
    """Open ``path`` read-only without following a final symlink or blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise UnsafeFileError(f"{path}: is a symlink") from None
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeFileError(f"{path}: not a regular file")
    return fd


def read_bytes_bounded(path: Path, max_bytes: int = DEFAULT_MAX_FILE) -> bytes:
    fd = open_regular(path)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafeFileError(f"{path}: larger than {max_bytes} bytes")
    return data


def read_text_bounded(path: Path, max_bytes: int = DEFAULT_MAX_FILE) -> str:
    return read_bytes_bounded(path, max_bytes).decode("utf-8", errors="replace")


def load_json_bounded(path: Path, max_bytes: int = DEFAULT_MAX_FILE) -> Any:
    """Parse a JSON file. Raises ``ValueError`` on invalid JSON."""
    return json.loads(read_text_bounded(path, max_bytes))


def load_toml_bounded(path: Path, max_bytes: int = DEFAULT_MAX_FILE) -> dict[str, Any]:
    """Parse a TOML file. Raises ``ValueError`` (``TOMLDecodeError``) on invalid TOML."""
    return tomllib.loads(read_text_bounded(path, max_bytes))


def _parse_line(raw: bytes, stats: ReadStats) -> tuple[bool, Any]:
    line = raw.strip()
    if not line:
        return False, None
    stats.lines += 1
    try:
        return True, json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        stats.parse_errors += 1
        return False, None


def iter_jsonl(
    path: Path,
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE,
    max_lines: int | None = None,
    stats: ReadStats | None = None,
) -> Iterator[Any]:
    """Yield parsed JSON values, one per line; skip and count oversized or malformed lines."""
    stats = stats if stats is not None else ReadStats()
    fd = open_regular(path)
    with os.fdopen(fd, "rb") as fh:
        count = 0
        while max_lines is None or count < max_lines:
            raw = fh.readline(max_line_bytes + 1)
            if not raw:
                return
            if len(raw) > max_line_bytes and not raw.endswith(b"\n"):
                stats.oversized += 1
                while (rest := fh.readline(max_line_bytes)) and not rest.endswith(b"\n"):
                    pass
                continue
            ok, value = _parse_line(raw, stats)
            if ok:
                count += 1
                yield value


@dataclass(frozen=True)
class FileCursor:
    """Where an incremental reader stopped in a file. ``skipping`` = inside an oversized line."""

    dev: int
    inode: int
    offset: int
    skipping: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"dev": self.dev, "inode": self.inode, "offset": self.offset, "skip": self.skipping}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileCursor:
        return cls(int(data["dev"]), int(data["inode"]), int(data["offset"]), bool(data["skip"]))


@dataclass
class TailResult:
    values: list[Any]
    cursor: FileCursor
    stats: ReadStats = field(default_factory=ReadStats)
    reset: bool = False  # file was rotated or truncated; reading restarted at 0
    more: bool = False  # budget exhausted before end of file


def tail_jsonl(
    path: Path,
    cursor: FileCursor | None,
    *,
    budget: int = DEFAULT_TAIL_BUDGET,
    max_line_bytes: int = DEFAULT_MAX_LINE,
) -> TailResult:
    """Read complete JSON lines appended since ``cursor``, at most ``budget`` bytes per call.

    A partial last line is left for the next call. Rotation (new inode) or truncation (size
    below the offset) restarts at 0. The returned cursor is only committed by the caller once
    the data derived from it is safely stored.
    """
    stats = ReadStats()
    budget = max(budget, max_line_bytes + 1)
    fd = open_regular(path)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        reset = False
        if cursor is None or (cursor.dev, cursor.inode) != (st.st_dev, st.st_ino):
            reset = cursor is not None
            cursor = FileCursor(st.st_dev, st.st_ino, 0)
        elif st.st_size < cursor.offset:
            reset = True
            cursor = FileCursor(st.st_dev, st.st_ino, 0)
        fh.seek(cursor.offset)
        buf = fh.read(budget)
        offset, skipping = cursor.offset, cursor.skipping
        values: list[Any] = []
        pos = 0
        if skipping:
            nl = buf.find(b"\n")
            if nl < 0:
                return TailResult(
                    [], FileCursor(st.st_dev, st.st_ino, offset + len(buf), True), stats, reset
                )
            pos, skipping = nl + 1, False
        while True:
            nl = buf.find(b"\n", pos)
            if nl < 0:
                break
            line = buf[pos : nl + 1]
            pos = nl + 1
            if len(line) > max_line_bytes:
                stats.oversized += 1
                continue
            ok, value = _parse_line(line, stats)
            if ok:
                values.append(value)
        remainder = len(buf) - pos
        if remainder > max_line_bytes:
            # an unfinished line already too long: skip it, resume after its newline
            stats.oversized += 1
            pos, skipping = len(buf), True
        more = len(buf) == budget and offset + len(buf) < st.st_size
        new_cursor = FileCursor(st.st_dev, st.st_ino, offset + pos, skipping)
        return TailResult(values, new_cursor, stats, reset, more)


def open_sqlite_readonly(path: Path, timeout: float = 0.5) -> sqlite3.Connection:
    """Open another program's SQLite DB read-only; writes fail, locks are short-lived."""
    if path.is_symlink() or not path.is_file():
        raise UnsafeFileError(f"{path}: not a regular file")
    uri = f"file:{quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    conn.execute("PRAGMA query_only = 1")
    return conn


def resolve_within(root: Path, relative: str | Path) -> Path:
    """Join ``relative`` onto ``root``, refusing any result (symlinks included) outside root."""
    root_resolved = root.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise UnsafeFileError(f"{relative}: escapes {root}")
    return candidate


def walk_bounded(
    root: Path,
    pattern: str = "*",
    *,
    max_depth: int = 6,
    max_entries: int = 20_000,
) -> Iterator[Path]:
    """Yield regular files under ``root`` whose name matches ``pattern``.

    Symlinks are never followed or yielded. Stops after ``max_entries`` directory entries.
    Order is deterministic (sorted per directory).
    """
    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        subdirs: list[tuple[Path, int]] = []
        for entry in reversed(entries):
            seen += 1
            if seen > max_entries:
                return
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth < max_depth:
                    subdirs.append((Path(entry.path), depth + 1))
            elif entry.is_file(follow_symlinks=False) and fnmatch.fnmatchcase(entry.name, pattern):
                yield Path(entry.path)
        stack.extend(reversed(subdirs))
