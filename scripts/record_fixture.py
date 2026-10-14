#!/usr/bin/env python3
"""Record a scrubbed test fixture from a real agent install (dev tool, not shipped).

    uv run scripts/record_fixture.py <agent-type> <source-dir> <dest-dir> [options]

Copies an allow-listed sample of the agent's files, then scrubs them: secrets are redacted,
the home path, user name, host name, e-mails and IPs are replaced by placeholders, and free text
(prompts, outputs, tool I/O) is replaced by length-class placeholders. Structure, ids,
timestamps, model names and usage numbers are kept. Credential files are never copied.
The output is checked a second time; if any secret pattern still matches, nothing is written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import getpass
import json
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from openvigie.redact import is_sensitive_key, redact
from openvigie.safeio import iter_jsonl, read_text_bounded, walk_bounded

INCLUDE: dict[str, list[str]] = {
    "claude-code": ["settings.json", "projects/*/*.jsonl", "projects/*/*/subagents/*.jsonl"],
    "codex": ["config.toml", "sessions/*/*/*/rollout-*.jsonl"],
    "hermes": ["config.yaml", "sessions/*", "logs/*.log", "*.db"],
    "openclaw": ["openclaw.json", "agents/*/sessions/*.jsonl", "agents/*/agent.json"],
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
]
# Every string below one of these keys is free text: replaced, except STRUCTURAL_KEYS.
CONTENT_KEYS = {
    "text",
    "content",
    "thinking",
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
LONG_TEXT = 120
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(?!127\.)(?:\d{1,3}\.){3}\d{1,3}\b")


class Scrubber:
    def __init__(self, home: str, user: str, host: str) -> None:
        self.replacements = [(home, "/home/user")]
        if user and len(user) > 2:
            self.replacements.append((user, "user"))
        if host and len(host) > 2:
            self.replacements.append((host, "host"))

    def text(self, value: str) -> str:
        value = redact(value)
        for old, new in self.replacements:
            value = value.replace(old, new)
        value = EMAIL.sub("user@example.com", value)
        return IPV4.sub("192.0.2.1", value)

    def path(self, rel: Path) -> Path:
        """Scrub user and host names out of path components (Claude Code encodes cwd in dirs)."""
        parts = []
        for part in rel.parts:
            for old, new in self.replacements[1:]:
                part = part.replace(old, new)
            parts.append(part)
        return Path(*parts)

    @staticmethod
    def placeholder(value: str) -> str:
        n = len(value)
        bucket = next((b for b in (16, 128, 1024, 8192) if n <= b), 65536)
        return f"[scrubbed text <= {bucket} chars]"

    def value(self, value: Any, key: str | None = None, in_content: bool = False) -> Any:
        in_content = in_content or key in CONTENT_KEYS
        if isinstance(value, str):
            if key and is_sensitive_key(key) and value:
                return "[REDACTED:key]"
            if in_content and key not in STRUCTURAL_KEYS:
                return self.placeholder(value) if value else value
            if len(value) > LONG_TEXT:
                return self.placeholder(value)
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v, k, in_content) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key, in_content) for v in value]
        return value


def denied(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name.lower(), pat) for pat in DENY)


def select_files(
    source: Path, patterns: list[str], max_files: int
) -> tuple[list[Path], list[Path]]:
    chosen: list[Path] = []
    dropped: list[Path] = []
    for path in walk_bounded(source, "*", max_depth=6):
        rel = path.relative_to(source)
        if not any(fnmatch.fnmatch(str(rel), pat) for pat in patterns):
            continue
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
        data = json.loads(read_text_bounded(src))
        dst.write_text(json.dumps(scrubber.value(data), indent=2) + "\n")
        return "json"
    if src.suffix == ".db":
        return scrub_sqlite(src, dst, scrubber)
    lines = read_text_bounded(src).splitlines()[:max_lines]
    dst.write_text("".join(scrubber.text(line) + "\n" for line in lines))
    return f"{len(lines)} lines"


def scrub_sqlite(src: Path, dst: Path, scrubber: Scrubber) -> str:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
        tables = [
            r[0] for r in target.execute("select name from sqlite_master where type = 'table'")
        ]
        target.create_function("scrub", 2, lambda v, k: scrubber.value(v, k))
        for table in tables:
            for _, col, coltype, *_ in target.execute(f'pragma table_info("{table}")'):
                if "CHAR" in coltype.upper() or "TEXT" in coltype.upper() or not coltype:
                    target.execute(
                        # names come from the schema of the DB we just copied
                        f'update "{table}" set "{col}" = scrub("{col}", ?) '  # noqa: S608
                        f"where typeof(\"{col}\") = 'text'",
                        (col.lower(),),
                    )
        target.commit()
        target.execute("vacuum")
    finally:
        source.close()
        target.close()
    return f"sqlite, {len(tables)} tables"


def leftover_secrets(root: Path) -> list[str]:
    hits = []
    for path in walk_bounded(root, "*", max_depth=8):
        if path.suffix == ".db":
            text = path.read_bytes().decode("utf-8", errors="replace")
        else:
            text = read_text_bounded(path, max_bytes=64 * 1024 * 1024)
        if redact(text) != text:
            hits.append(str(path.relative_to(root)))
    return hits


MANIFEST = """# Fixture: {agent} ({date})

Recorded with `scripts/record_fixture.py` from a real install, then scrubbed.

## Files

{files}

## Dropped (never copied)

{dropped}

## To fill in by hand

- Agent version:
- How it runs (CLI, gateway, systemd unit, Docker):
- Process name / command-line signature:
- Listening ports:
- Where token usage is recorded (file, field names):
- Expected totals per session (for contract tests):

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
    scrubber: Scrubber | None = None,
) -> Path:
    if agent not in INCLUDE:
        raise SystemExit(f"unknown agent type {agent!r}; expected one of {sorted(INCLUDE)}")
    patterns = INCLUDE[agent] + (extra_include or [])
    scrubber = scrubber or Scrubber(str(Path.home()), getpass.getuser(), socket.gethostname())
    chosen, dropped = select_files(source, patterns, max_files)
    if not chosen:
        raise SystemExit(f"no files matched {patterns} under {source}")
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "fixture"
        notes = [
            f"- `{scrubber.path(rel)}`: "
            + scrub_file(source / rel, staging / scrubber.path(rel), scrubber, max_lines)
            for rel in chosen
        ]
        hits = leftover_secrets(staging)
        if hits:
            raise SystemExit(f"secret patterns still match after scrubbing: {hits}; not written")
        (staging / "MANIFEST.md").write_text(
            MANIFEST.format(
                agent=agent,
                date=dt.date.today().isoformat(),
                files="\n".join(notes),
                dropped="\n".join(f"- `{scrubber.path(d)}`" for d in dropped) or "- none",
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
    parser.add_argument("--max-lines", type=int, default=200)
    parser.add_argument("--include", action="append", default=[], help="extra glob, repeatable")
    args = parser.parse_args(argv)
    dest = record(
        args.agent,
        args.source.expanduser(),
        args.dest,
        max_files=args.max_files,
        max_lines=args.max_lines,
        extra_include=args.include,
    )
    print(f"wrote {dest}. Review {dest / 'MANIFEST.md'} before committing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
