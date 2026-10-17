#!/usr/bin/env python3
"""Record a scrubbed test fixture from a real agent install (dev tool, not shipped).

    uv run scripts/record_fixture.py <agent-type> <source-dir> <dest-dir> [options]

Copies an allow-listed sample of the agent's files, then scrubs them: secrets are redacted,
the home path, user name, host name, e-mails and IPs are replaced by placeholders, and free text
(prompts, outputs, tool I/O) is replaced by length-class placeholders. Structure, ids,
timestamps, model names and usage numbers are kept. Credential files are never copied.

It also writes SURVEY.md: every file under the source (path, size, age; no content), and the
agent's running processes and listening ports, scrubbed the same way. That is what the adapter
needs beyond the samples (process signature, ports, where usage lives).
The output is checked a second time; if any secret pattern still matches, nothing is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import getpass
import itertools
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from collections import deque
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from openvigie.redact import is_sensitive_key, redact
from openvigie.safeio import iter_jsonl, read_text_bounded, walk_bounded

INCLUDE: dict[str, list[str]] = {
    "claude-code": ["settings.json", "projects/*/*.jsonl", "projects/*/*/subagents/*.jsonl"],
    "codex": ["config.toml", "sessions/*/*/*/rollout-*.jsonl"],
    "hermes": [
        # first: patterns are taken in order until --max-files
        "config.yaml",
        "gateway_state.json",
        "gateway.pid",
        # the adapter's store; kanban.db and verification_evidence.db hold project paths
        "state.db",
        "sessions/*",
        "sessions/*/*",
        "logs/*.log",
    ],
    "openclaw": [
        # first: patterns are taken in order until --max-files
        "openclaw.json",
        "agents/*/agent/*.sqlite",  # OpenClaw's session store
        "agents/*/agent.json",
        "agents/*/sessions/*.jsonl",
        "agents/*/sessions/*.json",
        "logs/*.log",
    ],
}
# process names / command-line words that identify each agent, for the survey
SIGNATURES: dict[str, tuple[str, ...]] = {
    "claude-code": ("claude",),
    "codex": ("codex",),
    "hermes": ("hermes",),
    "openclaw": ("openclaw", "clawdbot", "moltbot"),
}
DENY = [
    ".env",
    ".env.*",
    "*.env",
    "auth.json",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*credential*",
    "*secret*",
    "*token*",
    "*.keychain*",
    "*cookie*",
    "auth*",
    "*oauth*",
    "*password*",
]
# Every string below one of these keys is free text: replaced, except STRUCTURAL_KEYS.
CONTENT_KEYS = {
    "text",
    "content",
    "thinking",
    "reasoning",
    "reasoning_content",
    "api_content",
    "assistantTexts",
    "toolMetas",
    "label",
    "display_name",
    "displayName",
    "ownerDisplay",
    "prompt",
    "input",
    "output",
    "arguments",
    "result",
    "stdout",
    "stderr",
    "summary",
    "body",
    "cwd",
    "message",
    "toolUseResult",
    "attachment",
    "rendered",
    "lastPrompt",
    "aiTitle",
    "title",
    "hookAdditionalContext",
    "hookInfos",
    "wireToolInputs",
    "gitBranch",
}
STRUCTURAL_KEYS = {"type", "id", "name", "role", "model", "tool_use_id", "stop_reason"}
# keys containing one of these words hold free text too (system_prompt, persona, soul_text…)
CONTENT_WORDS = ("prompt", "instruction", "persona", "personality", "soul", "description",
                 "message", "note", "memory")  # fmt: skip
# recorder-only and deliberately broad: a secret-looking key anywhere on a line, any quoting
LOOSE_SECRET = re.compile(
    r"(?i)([\w-]*(?:key|token|secret|passw(?:or)?d|auth)[\w-]*[\"']?[ \t]*[:=][ \t]*)"
    r"(?:\"(?!\[REDACTED)[^\"]{4,}\"|'(?!\[REDACTED)[^']{4,}'|(?![\"'\[{])(?!\d+\b)[^\s,}\]]{4,})"
)
# Known non-secrets that LOOSE_SECRET catches, seen on real installs (#55)
NOT_A_VALUE = {"true", "false", "null", "none"}
ENV_NAME = re.compile(
    r"[A-Z][A-Z0-9_]{2,}"
)  # under a *_env key: the variable's name, not its value
KEY_COMBO = re.compile(r"(?i)(?:ctrl|alt|shift|cmd|meta|super|option)(?:\+[\w-]+)+")
PEER_DIGITS = re.compile(r"[+-]?\d{5,}")
ROUTING_KEYS = {"sessionkey", "session_key"}  # OpenClaw's agent:<id>:<channel>:<kind>:<peer>
SESSION_KEY = re.compile(r"\bagent:[\w.@+:-]{1,200}")
PEER_ID = re.compile(r"(?<=:)[+-]?\d{5,}(?![\w])")  # chat ids, phone numbers
# chat and user ids are personal: found before scrubbing, then replaced wherever they appear
PEER_KEY = re.compile(r"(?i)^(?:chat|user|sender|from|peer|recipient|owner)_?id$")
# lists of people allowed to reach the agent; narrow on purpose (an "allowed ports" list isn't)
ALLOW_KEY = re.compile(
    r"(?i)(?:allow_?from|allowed_?(?:users|chats|ids|senders|peers)|allow_?list|white_?list"
    r"|trusted_?(?:users|ids)|admins?$|owners?$)"
)
# in keys and file names, long numbers are chat, group or bot ids; dates (8 digits) are kept
LONG_ID = re.compile(r"(?<!\d)\d{9,}(?!\d)")
TELEGRAM_GROUP = re.compile(r"(?<![\d-])-100\d{10}(?!\d)")  # a supergroup id, anywhere
SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SCRUBBED_EMAIL = re.compile(r"(?:[\w.-]{1,40}:)?user@example\.com")
WHOLE_DIGITS = re.compile(r"(?<!\d)\d{5,}(?!\d)")  # in tg:<id>, +<phone>, -100<group>
PEER_FIELD = re.compile(
    r"(?i)\b(?:chat|user|sender|from|peer|recipient|owner)_?id[\"']?[ \t]*[:=][ \t]*"
    r"[\"']?[+-]?(\d{5,})\b"
)
# broader names (home_channel, bot_id, thread…): long numbers only, and never a timestamp
BROAD_PEER_WORDS = r"(?:chat|user|sender|peer|channel|group|bot|thread|topic)"
BROAD_PEER_FIELD = re.compile(
    rf"(?i)\b[\w-]*{BROAD_PEER_WORDS}[\w-]*[\"']?[ \t]*[:=][ \t]*[\"']?[+-]?(\d{{9,}})\b"
)
BROAD_PEER_COLUMN = re.compile(rf"(?i){BROAD_PEER_WORDS}")
LONG_TEXT = 120
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(?!127\.)(?:\d{1,3}\.){3}\d{1,3}\b")
TAILNET = re.compile(r"\b[\w.-]+\.ts\.net\b")  # MagicDNS names the tailnet, not just the host
# a note's file name says what it is about; dated notes (OpenClaw's daily memory) say nothing
MEMORY_NOTE = re.compile(r"(?<![\w.-])(memory/(?:[\w.-]+/)*)([^/\n\"'`\\]{1,120}?\.md)(?![\w])")
DATED_NOTE = re.compile(r"\d{4}-\d{2}-\d{2}(?:-\d{4})?\.md")
SCRUBBED_NOTE = re.compile(r"note-\d+\.md")
HEX_TOKEN = re.compile(r"(?i)(?=[0-9a-f-]*\d)[0-9a-f-]+")  # a uuid or hash: "dede" in it is no name


class Scrubber:
    def __init__(self, home: str, user: str, host: str) -> None:
        self.replacements = [(home, "/home/user")]
        if user and len(user) > 2:
            self.replacements.append((user, "user"))
        if host and len(host) > 2:
            self.replacements.append((host, "host"))
        self.peers: dict[str, str] = {}  # digits -> placeholder digits
        self.peer_re: re.Pattern[str] | None = None
        self.names: list[tuple[re.Pattern[str], str]] = []
        self.notes: dict[str, str] = {}

    def add_names(self, names: Iterable[str], label: str) -> None:
        """Between any non-alphanumerics, with any of ``-``, ``_`` or a space between parts:
        ``tax-return`` also matches ``Tax Return`` and ``workspace_tax_return``, and ``cafe`` not
        the hash ``3cafe4`` nor the uuid segment ``12-cafe-34``."""
        known = {placeholder for _, placeholder in self.names}
        for name in sorted(set(names) - {""}, key=lambda n: (-len(n), n)):
            parts = r"[-_ ]?".join(map(re.escape, re.split(r"[-_ ]+", name.strip("-_ "))))
            n = sum(p.startswith(f"{label}-") for p in known) + 1
            known.add(placeholder := f"{label}-{n}")
            self.names.append((re.compile(rf"(?i)(?<![^\W_]){parts}(?![^\W_])"), placeholder))

    def swap_names(self, text: str) -> str:
        for pattern, placeholder in self.names:

            def swap(m: re.Match[str], placeholder: str = placeholder) -> str:
                return m.group(0) if in_hex(m) else placeholder

            text = pattern.sub(swap, text)
        return MEMORY_NOTE.sub(self.note, text)

    def names_left(self, text: str) -> bool:
        """The last check: a name not in a uuid or hash, or a note name we didn't replace."""
        return any(not in_hex(m) for p, _ in self.names for m in p.finditer(text)) or any(
            not (DATED_NOTE.fullmatch(m.group(2)) or SCRUBBED_NOTE.fullmatch(m.group(2)))
            for m in MEMORY_NOTE.finditer(text)
        )

    def note(self, match: re.Match[str]) -> str:
        folder, name = match.groups()
        if DATED_NOTE.fullmatch(name):
            return match.group(0)
        return folder + self.notes.setdefault(name, f"note-{len(self.notes) + 1}.md")

    def add_peers(self, ids: set[str]) -> None:
        for digits in sorted(ids):
            self.peer_digits(digits)
        if self.peers:
            self.peer_re = whole_numbers(self.peers)

    def swap_peers(self, text: str) -> str:
        """Whole numbers only: an id inside a timestamp or a token count is left alone."""
        if self.peer_re is None:
            return text
        return self.peer_re.sub(lambda m: self.peers[m.group(0)], text)

    def peer_digits(self, digits: str) -> str:
        """The same chat id or phone number always gets the same placeholder."""
        return self.peers.setdefault(digits, str(100000001 + len(self.peers)))

    def text(self, value: str) -> str:
        value = SESSION_KEY.sub(lambda m: PEER_ID.sub(self.peer, m.group(0)), value)
        value = LOOSE_SECRET.sub(
            lambda m: m.group(0) if benign(m) else f'{m.group(1)}"[REDACTED:key]"', redact(value)
        )
        for old, new in self.replacements:
            value = value.replace(old, new)
        value = self.swap_names(self.swap_peers(value))
        value = EMAIL.sub("user@example.com", value)
        value = TAILNET.sub("host.example.ts.net", value)
        return IPV4.sub("192.0.2.1", value)

    def peer(self, match: re.Match[str]) -> str:
        found = match.group(0)
        sign = found[0] if found[0] in "+-" else ""
        return sign + self.peer_digits(found.lstrip("+-"))

    def key(self, key: Any) -> Any:
        """Keys are scrubbed like values: e-mails, ids, home and host names."""
        return self.text(key) if isinstance(key, str) else self.value(key)

    def path(self, rel: Path) -> Path:
        """Scrub user and host names out of path components (Claude Code encodes cwd in dirs)."""
        parts = []
        for part in rel.parts:
            for old, new in self.replacements[1:]:
                part = part.replace(old, new)
            parts.append(self.swap_names(self.swap_peers(part)))
        return Path(*parts)

    @staticmethod
    def placeholder(value: str) -> str:
        n = len(value)
        bucket = next((b for b in (16, 128, 1024, 8192) if n <= b), 65536)
        return f"[scrubbed text <= {bucket} chars]"

    def value(self, value: Any, key: str | None = None, in_content: bool = False) -> Any:
        in_content = (
            in_content
            or key in CONTENT_KEYS
            or (key is not None and any(word in key.lower() for word in CONTENT_WORDS))
        )
        if isinstance(value, str):
            if key and is_sensitive_key(key) and value:
                return "[REDACTED:key]"
            if in_content and key not in STRUCTURAL_KEYS:
                return self.placeholder(value) if value else value
            if len(value) > LONG_TEXT:
                return self.placeholder(value)
            return self.text(value)
        if isinstance(value, int) and str(abs(value)) in self.peers:
            return int(self.peers[str(abs(value))]) * (-1 if value < 0 else 1)
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for k, v in value.items():
                new = self.key(k)
                while new in out:  # two keys scrubbed alike (two e-mails): keep both
                    new = f"{new} #{len(out)}"
                out[new] = self.value(v, k, in_content)
            return out
        if isinstance(value, list):
            return [self.value(v, key, in_content) for v in value]
        return value


def benign(match: re.Match[str]) -> bool:
    """A LOOSE_SECRET match that is no secret: a boolean, a scrubber placeholder, an env var
    name under a *_env key, a key binding, or a session routing key (its peer ids are replaced
    by the Scrubber)."""
    key_match = re.match(r"[\w-]*", match.group(1))
    key = key_match.group(0).lower() if key_match else ""
    value = match.group(0)[len(match.group(1)) :].strip("\"'")
    return (
        value.lower() in NOT_A_VALUE
        or value.startswith("[scrubbed text <=")  # our own placeholder
        or SCRUBBED_EMAIL.fullmatch(value) is not None  # authProfileId: "openai:<e-mail>"
        or (key.endswith("_env") and ENV_NAME.fullmatch(value) is not None)
        or KEY_COMBO.fullmatch(value) is not None
        or (key in ROUTING_KEYS and value.startswith("agent:"))
    )


def has_secret(text: str) -> bool:
    return redact(text) != text or any(not benign(m) for m in LOOSE_SECRET.finditer(text))


def denied(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name.lower(), pat) for pat in DENY)


def select_files(
    source: Path, patterns: list[str], max_files: int
) -> tuple[list[Path], list[Path]]:
    """Each pattern is walked from its literal folder, to its own depth (``*.db`` is the top
    level only), so a big unrelated folder (a git checkout) can't use up the walk's budget."""
    chosen: list[Path] = []
    dropped: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        parts = Path(pattern).parts
        literal = list(itertools.takewhile(lambda p: not WILDCARD.search(p), parts[:-1]))
        base = source.joinpath(*literal)
        if any(source.joinpath(*literal[: n + 1]).is_symlink() for n in range(len(literal))):
            continue  # never walk out of the source
        for path in walk_bounded(base, "*", max_depth=len(parts) - len(literal) - 1):
            rel = path.relative_to(source)
            if rel in seen or len(rel.parts) != len(parts):
                continue
            if not fnmatch.fnmatch(str(rel), pattern):
                continue
            seen.add(rel)
            if denied(path):
                dropped.append(rel)
            elif len(chosen) < max_files:
                chosen.append(rel)
    return chosen, dropped


def scrub_file(src: Path, dst: Path, scrubber: Scrubber, max_lines: int) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".jsonl":
        values = list(iter_jsonl(src, max_lines=max_lines))
        dst.write_text("".join(json.dumps(scrubber.value(v)) + "\n" for v in values))
        return f"{len(values)} lines"
    if src.suffix == ".json":
        text = read_text_bounded(src)
        try:
            data, kind = json.loads(text), "json"
        except ValueError:  # JSON5 (OpenClaw): comments out, then YAML reads the rest
            data, kind = json5_data(text), "JSON5, written as JSON (comments dropped)"
        dst.write_text(json.dumps(scrubber.value(data), indent=2) + "\n")
        return kind
    if src.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(read_text_bounded(src))
        dst.write_text(yaml.safe_dump(scrubber.value(data), sort_keys=False))
        return "yaml (comments dropped)"
    if src.suffix in SQLITE_SUFFIXES:
        return scrub_sqlite(src, dst, scrubber, max_rows=max_lines)
    lines = read_text_bounded(src).splitlines()[:max_lines]
    dst.write_text("".join(scrubber.text(line) + "\n" for line in lines))
    return f"{len(lines)} lines, only pattern-redacted: **check it by hand**"


def json5_data(text: str) -> Any:
    """Parse JSON5 well enough for a fixture: drop // and /* */ comments outside strings, then
    let YAML read it (it takes unquoted keys, single quotes and trailing commas)."""
    import yaml

    out, i, quote = [], 0, ""
    while i < len(text):
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
            out.append(c)
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = len(text) if i < 0 else i
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    try:
        return yaml.safe_load("".join(out))
    except yaml.YAMLError as exc:
        raise SystemExit(f"could not read a JSON5 file: {exc.__class__.__name__}") from None


def scrub_sqlite(src: Path, dst: Path, scrubber: Scrubber, max_rows: int) -> str:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        notes = drop_indexes_and_triggers(target)
        tables = [
            r[0] for r in target.execute("select name from sqlite_master where type = 'table'")
        ]
        notes += keep_newest_rows(target, tables, max_rows)
        target.create_function("scrub", 2, lambda v, k: scrubber.value(v, k))
        for table in tables:
            for _, col, coltype, *_ in target.execute(f'pragma table_info("{table}")'):
                textual = "CHAR" in coltype.upper() or "TEXT" in coltype.upper() or not coltype
                if textual or PEER_KEY.match(col):
                    scrub_column(target, table, col)
        target.commit()
        target.execute("vacuum")
        # a WAL-mode copy grows -wal/-shm files next to it whenever it's opened
        target.execute("pragma journal_mode=delete")
    finally:
        source.close()
        target.close()
    return "; ".join([f"sqlite, {len(tables)} tables", *notes])


def drop_indexes_and_triggers(db: sqlite3.Connection) -> list[str]:
    """Full-text indexes keep the original terms (prompt fragments, ids) after their content is
    scrubbed, and triggers would re-index while we scrub. ANALYZE's sqlite_stat tables hold
    sample values of indexed columns as blobs, which aren't scrubbed. Adapters need none."""
    virtual = [
        r[0]
        for r in db.execute(
            "select name from sqlite_master where type = 'table' "
            "and upper(sql) like 'CREATE VIRTUAL TABLE%'"
        )
    ]
    triggers = [r[0] for r in db.execute("select name from sqlite_master where type = 'trigger'")]
    stats = [
        r[0]
        for r in db.execute(
            "select name from sqlite_master where type = 'table' and name like 'sqlite_stat%'"
        )
    ]
    try:
        for name in stats:
            db.execute(f'drop table "{name}"')
        for name in triggers:
            db.execute(f'drop trigger "{name}"')
        for name in virtual:  # its shadow tables go with it
            db.execute(f'drop table "{name}"')
    except sqlite3.OperationalError as exc:  # e.g. the FTS module isn't built in
        raise SystemExit(f"could not drop a full-text index: {exc}; not written") from None
    db.commit()
    notes = [f"dropped full-text index {', '.join(virtual)}"] if virtual else []
    if triggers:
        notes.append(f"dropped {len(triggers)} trigger{'s' if len(triggers) > 1 else ''}")
    return notes


def keep_newest_rows(db: sqlite3.Connection, tables: list[str], max_rows: int) -> list[str]:
    """A sample, not the whole history: the newest rows by rowid (tables without one are kept)."""
    notes = []
    for table in tables:
        total = db.execute(f'select count(*) from "{table}"').fetchone()[0]  # noqa: S608
        if total <= max_rows:
            continue
        try:
            db.execute(
                f'delete from "{table}" where rowid not in '  # noqa: S608
                f'(select rowid from "{table}" order by rowid desc limit ?)',
                (max_rows,),
            )
        except sqlite3.OperationalError:  # WITHOUT ROWID
            continue
        notes.append(f"newest {max_rows} of {total} rows in {table}")
    db.commit()
    return notes


def scrub_column(db: sqlite3.Connection, table: str, col: str) -> None:
    # names come from the schema of the DB we just copied. OR ABORT overrides the table's own
    # ON CONFLICT clause: rollback would undo earlier scrubs, ignore and replace keep raw text
    # or drop rows. With it a collision always raises and undoes only this statement.
    try:
        db.execute(
            f'update or abort "{table}" set "{col}" = scrub("{col}", ?) '  # noqa: S608
            f"where typeof(\"{col}\") in ('text', 'integer')",
            (col.lower(),),
        )
    except sqlite3.IntegrityError:
        # a UNIQUE column: equal placeholders collide, so tell them apart by rowid
        try:
            db.execute(
                f'update or abort "{table}" set "{col}" = '  # noqa: S608
                f"scrub(\"{col}\", ?) || ' #' || rowid where typeof(\"{col}\") = 'text'",
                (col.lower(),),
            )
        except sqlite3.OperationalError:
            raise SystemExit(
                f"table {table} has a unique text column ({col}) and no rowid; not written"
            ) from None


def whole_numbers(ids: Iterable[str]) -> re.Pattern[str]:
    alternatives = "|".join(sorted(map(re.escape, ids), key=len, reverse=True))
    return re.compile(rf"(?<!\d)(?:{alternatives})(?!\d)")


def collect_peers(source: Path, files: list[Path]) -> set[str]:
    """Chat and user ids in the sample: peers in session keys, values of chat_id-like fields
    and columns, and entries of allow lists. Collected first so every file replaces them the
    same way."""
    ids: set[str] = set()
    for rel in files:
        path = source / rel
        if path.suffix in SQLITE_SUFFIXES:
            ids |= db_peers(path)
            continue
        text = read_text_bounded(path, max_bytes=64 * 1024 * 1024)
        for key in SESSION_KEY.finditer(text):
            ids |= {m.group(0).lstrip("+-") for m in PEER_ID.finditer(key.group(0))}
        ids |= {m.group(1) for m in PEER_FIELD.finditer(text)}
        ids |= {m.group(0).lstrip("-") for m in TELEGRAM_GROUP.finditer(text)}
        ids |= {d for m in BROAD_PEER_FIELD.finditer(text) if not timestamp(d := m.group(1))}
        for data in structured(path, text):
            ids |= allow_list_peers(data)
    return ids


def structured(path: Path, text: str) -> list[Any]:
    """The parsed file for walking by key; nothing if it doesn't parse (scrub_file will say)."""
    import yaml

    try:
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        if path.suffix == ".json":
            try:
                return [json.loads(text)]
            except ValueError:
                return [json5_data(text)]
        if path.suffix in (".yaml", ".yml"):
            return [yaml.safe_load(text)]
    except (ValueError, SystemExit, yaml.YAMLError):
        pass
    return []


def timestamp(digits: str) -> bool:
    """Epoch seconds, ms, µs or ns between 2017 and 2036: not an id."""
    n = int(digits)
    return any(1_500_000_000 * 1000**k <= n <= 2_100_000_000 * 1000**k for k in range(4))


def allow_list_peers(data: Any, listed: bool = False) -> set[str]:
    """Entries of allow lists, and long ids used as keys (``groups: {"-100…": …}``)."""
    if isinstance(data, dict):
        in_keys = {d for k in data for d in LONG_ID.findall(str(k))}
        return in_keys.union(
            *(
                allow_list_peers(v, listed or bool(ALLOW_KEY.search(str(k))))
                for k, v in data.items()
            )
        )
    if isinstance(data, list):
        return set().union(*(allow_list_peers(v, listed) for v in data))
    if listed and isinstance(data, (str, int)) and not isinstance(data, bool):
        return set(WHOLE_DIGITS.findall(str(data)))
    return set()


def db_peers(path: Path) -> set[str]:
    ids: set[str] = set()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in db.execute("select name from sqlite_master where type = 'table'")]
        for table in tables:
            for _, col, *_ in db.execute(f'pragma table_info("{table}")').fetchall():
                if PEER_KEY.match(col):
                    rows = db.execute(f'select distinct "{col}" from "{table}"')  # noqa: S608
                    ids |= {str(v).lstrip("+-") for (v,) in rows if PEER_DIGITS.fullmatch(str(v))}
                elif BROAD_PEER_COLUMN.search(col):
                    rows = db.execute(f'select distinct "{col}" from "{table}"')  # noqa: S608
                    ids |= {
                        d for (v,) in rows
                        if re.fullmatch(r"[+-]?\d{9,}", str(v))
                        and not timestamp(d := str(v).lstrip("+-"))
                    }  # fmt: skip
    finally:
        db.close()
    return ids


def long_numbers(root: Path, placeholders: set[str], per_place: int = 12) -> list[str]:
    """Where numbers that could be a chat, group, bot or phone id remain, for review: 9 to 15
    digits, not a placeholder, not a timestamp. One line per file, or per table.column."""
    found: dict[tuple[str, str], set[str]] = {}

    def add(file: str, where: str, text: str) -> None:
        for d in LONG_ID.findall(text):
            if len(d) <= 15 and d not in placeholders and not timestamp(d):
                found.setdefault((file, where), set()).add(d)

    for path in walk_bounded(root, "*", max_depth=8):
        rel = str(path.relative_to(root))
        add(rel, "", rel)
        if path.suffix in SQLITE_SUFFIXES:
            for where, value in sqlite_values(path):
                add(rel, f" {where}", value)
        else:
            add(rel, "", read_text_bounded(path, max_bytes=64 * 1024 * 1024))
    lines = []
    for (file, where), numbers in sorted(found.items()):
        shown = sorted(numbers)[:per_place]
        more = f" (+{len(numbers) - per_place} more)" if len(numbers) > per_place else ""
        lines.append(f"- `{file}`{where}: " + ", ".join(f"`{d}`" for d in shown) + more)
    return lines


def sqlite_values(path: Path) -> Iterator[tuple[str, str]]:
    """Every text, blob and large integer value as text, with its table.column."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in db.execute("select name from sqlite_master where type = 'table'")]
        for table in tables:
            cols = [c[1] for c in db.execute(f'pragma table_info("{table}")')]
            for row in db.execute(f'select * from "{table}"'):  # noqa: S608
                for col, value in zip(cols, row, strict=False):
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    if isinstance(value, int) and not isinstance(value, bool):
                        value = str(abs(value))
                    if isinstance(value, str):
                        yield f"{table}.{col}", value
    finally:
        db.close()


def leftover_peers(root: Path, ids: set[str]) -> list[str]:
    """Files that still hold a collected id as a whole number anywhere in their bytes (a .db's
    raw pages too). Each file is read once."""
    if not ids:
        return []
    pattern = re.compile(whole_numbers(ids).pattern.encode())
    return [
        str(path.relative_to(root))
        for path in walk_bounded(root, "*", max_depth=8)
        if pattern.search(path.read_bytes())
    ]


def in_hex(match: re.Match[str]) -> bool:
    text, start, end = match.string, match.start(), match.end()
    while start and (text[start - 1].isalnum() or text[start - 1] in "-_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] in "-_"):
        end += 1
    return HEX_TOKEN.fullmatch(text, start, end) is not None


def leftover_names(root: Path, scrubber: Scrubber) -> list[str]:
    """Files that still hold a profile name, a private word or a note name in their bytes."""
    return [
        str(path.relative_to(root))
        for path in walk_bounded(root, "*", max_depth=8)
        if scrubber.names_left(
            f"{path.relative_to(root)}\n" + path.read_bytes().decode("utf-8", "replace")
        )
    ]


def text_has_secret(text: str) -> bool:
    """Line by line: redact() truncates past 1 MB, so a whole large file would always differ.
    Private keys span lines, so any key block fails closed."""
    return "PRIVATE KEY-----" in text or any(has_secret(line) for line in text.splitlines())


def db_has_secret(path: Path) -> bool:
    """Check each value on its own: raw file bytes run adjacent values together into false
    `key=value` matches. The schema is the agent's DDL, checked for real secret shapes only."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if db.execute("pragma freelist_count").fetchone()[0]:
            return True  # free pages aren't values we can read: fail closed
        for (sql,) in db.execute("select sql from sqlite_master where sql is not null"):
            if redact(sql) != sql:
                return True
        tables = [r[0] for r in db.execute("select name from sqlite_master where type = 'table'")]
        for table in tables:
            for row in db.execute(f'select * from "{table}"'):  # noqa: S608
                for value in row:
                    if isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                    if isinstance(value, str) and has_secret(value):
                        return True
        return False
    finally:
        db.close()


def leftover_secrets(root: Path) -> list[str]:
    hits = []
    for path in walk_bounded(root, "*", max_depth=8):
        if path.suffix in SQLITE_SUFFIXES:
            found = db_has_secret(path)
        else:
            found = text_has_secret(read_text_bounded(path, max_bytes=64 * 1024 * 1024))
        if found:
            hits.append(str(path.relative_to(root)))
    return hits


MAX_SURVEY_FILES = 400
SURVEY_DIR_FILES = 20
WILDCARD = re.compile(r"[*?\[]")
SENSITIVE_FLAG = re.compile(r"(?i)(key|token|secret|passw|auth|cookie|credential)")


def scrub_args(args: list[str], scrubber: Scrubber) -> list[str]:
    """A command line without secrets: values after --api-key-like flags and in KEY=value pairs
    are dropped, the rest is redacted and home, user and host names replaced."""
    out: list[str] = []
    for i, arg in enumerate(args):
        previous = args[i - 1] if i else ""
        if previous.startswith("-") and SENSITIVE_FLAG.search(previous) and "=" not in previous:
            out.append("[REDACTED]")
        elif "=" in arg and SENSITIVE_FLAG.search(arg.split("=", 1)[0]):
            out.append(arg.split("=", 1)[0] + "=[REDACTED]")
        else:
            text = scrubber.text(arg)
            out.append(text if len(text) <= LONG_TEXT else text[:LONG_TEXT] + "…")
    return out


def walk_breadth_first(
    source: Path, max_depth: int = 6, max_entries: int = 50_000
) -> Iterator[tuple[Path, int, list[os.DirEntry[str]]]]:
    """Each folder with its sorted entries, shallow folders first; symlinks are not followed."""
    queue: deque[tuple[Path, int]] = deque([(source, 0)])
    seen = 0
    while queue:
        directory, depth = queue.popleft()
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        seen += len(entries)
        if seen > max_entries:
            return
        for entry in entries:
            if depth < max_depth and entry.is_dir(follow_symlinks=False):
                queue.append((Path(entry.path), depth + 1))
        yield directory, depth, entries


def profile_names(source: Path) -> set[str]:
    """Hermes profiles are often named after people or projects: the ``profiles/`` folders and
    the gateway's ``served_profiles``. ``default`` says nothing."""
    names = {p.name for p in itertools.islice((source / "profiles").glob("*"), 500) if p.is_dir()}
    state = source / "gateway_state.json"
    if state.is_file():
        try:
            served = json.loads(read_text_bounded(state)).get("served_profiles")
        except (ValueError, AttributeError):
            served = None
        if isinstance(served, list):
            names |= {s for s in served if isinstance(s, str)}
    return names - {"default"}


def name_peers(source: Path) -> set[str]:
    """Long ids in file and folder names (``telegram_update_receipts_<bot id>.json``)."""
    return {
        d for _, _, entries in walk_breadth_first(source) for e in entries
        for d in LONG_ID.findall(e.name)
    }  # fmt: skip


def survey_files(source: Path, chosen: set[Path], scrubber: Scrubber, now: float) -> list[str]:
    """Breadth first, so top-level files and folders come before deep caches. A folder with
    more than SURVEY_DIR_FILES files (below the top level) is one summary line."""
    lines: list[str] = []
    for directory, depth, entries in walk_breadth_first(source):
        files = []
        for entry in entries:
            if entry.is_file(follow_symlinks=False):
                try:
                    files.append((Path(entry.path), entry.stat(follow_symlinks=False)))
                except OSError:
                    continue
        rel_dir = directory.relative_to(source)
        if depth > 0 and len(files) > SURVEY_DIR_FILES:
            lines.append(folder_line(rel_dir, files, chosen, scrubber, now, source))
        else:
            for path, st in files:
                rel = path.relative_to(source)
                mark = " (never copied)" if denied(path) else (" (copied)" if rel in chosen else "")
                lines.append(f"- `{scrubber.path(rel)}`: {st.st_size:,} bytes, "
                             f"{age(now, st.st_mtime)}{mark}")  # fmt: skip
        if len(lines) >= MAX_SURVEY_FILES:
            lines = [*lines[:MAX_SURVEY_FILES], f"- … more than {MAX_SURVEY_FILES} lines"]
            break
    return lines or ["- none"]


def folder_line(
    rel_dir: Path,
    files: list[tuple[Path, os.stat_result]],
    chosen: set[Path],
    scrubber: Scrubber,
    now: float,
    source: Path,
) -> str:
    copied = sum(p.relative_to(source) in chosen for p, _ in files)
    never = sum(denied(p) for p, _ in files)
    marks = ", ".join(x for x in (f"{copied} copied" if copied else "",
                                  f"{never} never copied" if never else "") if x)  # fmt: skip
    examples = ", ".join(f"`{scrubber.path(Path(p.name))}`" for p, _ in files[:3])
    return (
        f"- `{scrubber.path(rel_dir)}/`: {len(files):,} files, "
        f"{sum(st.st_size for _, st in files):,} bytes, newest "
        f"{age(now, max(st.st_mtime for _, st in files))}"
        f"{f' ({marks})' if marks else ''}; e.g. {examples}"
    )


def age(now: float, mtime: float) -> str:
    hours = max(0.0, (now - mtime) / 3600)
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"


def survey_processes(agent: str, scrubber: Scrubber, ps: Any) -> list[str]:
    words = SIGNATURES[agent]
    me = {os.getpid(), os.getppid()}
    lines = []
    for proc in ps.process_iter(["pid", "name", "cmdline"]):
        name, cmd = proc.info.get("name") or "", proc.info.get("cmdline") or []
        head = " ".join([name, *cmd[:3]]).lower()
        if proc.info["pid"] in me or "record_fixture" in head:
            continue
        if not any(word in head for word in words):
            continue
        try:
            ports = sorted({c.laddr.port for c in proc.net_connections(kind="inet")
                            if c.status == ps.CONN_LISTEN})  # fmt: skip
            listening = ", ".join(map(str, ports)) or "none"
        except (ps.AccessDenied, ps.NoSuchProcess):
            listening = "unknown (run as the agent's user)"
        args = " ".join(scrub_args(list(cmd[:8]), scrubber))
        lines.append(f"- `{scrubber.text(name)}`: `{args}`; listening on: {listening}")
    return lines or ["- none running (start the agent and run the recorder again to see it)"]


SURVEY = """# Survey: {agent} ({date})

Written by `scripts/record_fixture.py`. File names, sizes and ages only, never content.

## Files under the source directory

{files}

## Running processes that look like {agent}

{processes}
"""


MANIFEST = """# Fixture: {agent} ({date})

Recorded with `scripts/record_fixture.py` from a real install, then scrubbed.

## Files

{files}

## Dropped (never copied)

{dropped}

## Long numbers left in the output (check each one by hand)

Numbers of 9 to 15 digits that aren't a placeholder or a timestamp: hashes and tool ids are
expected; a chat, group, bot or phone number is not.

{numbers}

## To fill in by hand (SURVEY.md has the files, processes and ports seen)

- Agent version (`<agent> --version`):
- How it runs (CLI, gateway, systemd unit, Docker):
- Process name / command-line signature:
- Listening ports:
- Config and state paths, and the environment variables that move them:
- Where token usage is recorded (file, field names):
- Expected totals per session (for contract tests):
- Recorded while: idle / active session (record one of each if you can)

## Review checklist

- [ ] No API keys, tokens, passwords or cookies (the recorder checked secret patterns)
- [ ] No personal content: prompts, outputs, names, e-mails, chat contacts
- [ ] Home paths, user and host names replaced
- [ ] Usage numbers and ids kept intact
"""


def record(
    agent: str,
    source: Path,
    dest: Path,
    *,
    max_files: int = 20,
    max_lines: int = 200,
    extra_include: list[str] | None = None,
    words: list[str] | None = None,
    scrubber: Scrubber | None = None,
    ps: Any = None,
) -> Path:
    if agent not in INCLUDE:
        raise SystemExit(f"unknown agent type {agent!r}; expected one of {sorted(INCLUDE)}")
    patterns = INCLUDE[agent] + (extra_include or [])
    scrubber = scrubber or Scrubber(str(Path.home()), getpass.getuser(), socket.gethostname())
    scrubber.add_names(profile_names(source), "profile")
    scrubber.add_names(words or [], "word")
    chosen, dropped = select_files(source, patterns, max_files)
    if not chosen:
        raise SystemExit(f"no files matched {patterns} under {source}")
    # names of copied files too: name_peers' walk may stop before reaching them
    in_names = {d for rel in chosen for d in LONG_ID.findall(str(rel))}
    scrubber.add_peers(collect_peers(source, chosen) | name_peers(source) | in_names)
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "fixture"
        notes = [
            f"- `{scrubber.path(rel)}`: "
            + scrub_file(source / rel, staging / scrubber.path(rel), scrubber, max_lines)
            for rel in chosen
        ]
        import psutil

        (staging / "SURVEY.md").write_text(SURVEY.format(
            agent=agent, date=dt.date.today().isoformat(),
            files="\n".join(survey_files(source, set(chosen), scrubber, time.time())),
            processes="\n".join(survey_processes(agent, scrubber, ps or psutil)),
        ))  # fmt: skip
        hits = leftover_secrets(staging)
        if hits:
            raise SystemExit(f"secret patterns still match after scrubbing: {hits}; not written")
        hits = leftover_peers(staging, set(scrubber.peers))
        if hits:
            raise SystemExit(f"chat or user ids still present after scrubbing: {hits}; not written")
        hits = leftover_names(staging, scrubber)
        if hits:
            raise SystemExit(f"profile, note names or words still present after scrubbing: {hits}")
        (staging / "MANIFEST.md").write_text(
            MANIFEST.format(
                agent=agent,
                date=dt.date.today().isoformat(),
                files="\n".join(notes),
                dropped="\n".join(f"- `{scrubber.path(d)}`" for d in dropped) or "- none",
                numbers="\n".join(long_numbers(staging, set(scrubber.peers.values()))) or "- none",
            )
        )
        if dest.exists():
            raise SystemExit(f"{dest} already exists; remove it first")
        shutil.copytree(staging, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("agent", choices=sorted(INCLUDE))
    parser.add_argument("source", type=Path)
    parser.add_argument("dest", type=Path)
    parser.add_argument("--max-files", type=int, default=20)
    parser.add_argument("--max-lines", type=int, default=200, help="per file; rows per table")
    parser.add_argument("--include", action="append", default=[], help="extra glob, repeatable")
    parser.add_argument(
        "--scrub-word",
        action="append",
        default=[],
        help="a private word to replace (a project, a place), repeatable",
    )
    args = parser.parse_args(argv)
    dest = record(
        args.agent,
        args.source.expanduser(),
        args.dest,
        max_files=args.max_files,
        max_lines=args.max_lines,
        extra_include=args.include,
        words=args.scrub_word,
    )
    print(f"wrote {dest}. Review {dest / 'MANIFEST.md'} before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
