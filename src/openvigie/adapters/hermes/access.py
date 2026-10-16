"""Who may message a Hermes profile (FR-8, FR-9), counted from its access settings.

Offers ``read_access`` and ``Access.channel``. Hermes keeps access rules only in environment
variables, normally the profile's ``.env``, the same file as its API keys and bot tokens. So this
is the one place openvigie opens a credential file, under three rules:

* the file is read line by line, and a line's value is looked at only when its name is an access
  setting (``ACCESS_NAME``); every other line is skipped without being split;
* each allowlist entry is hashed at once, only to count distinct people, and a flag is reduced to
  true or false: no value from ``.env`` ever leaves this module;
* approvals from pairing (``platforms/pairing/<platform>-approved.json``, or ``pairing/``) are
  counted the same way.

Hermes' rules, per platform ``P``: ``P_ALLOW_ALL_USERS`` admits anyone. Otherwise a sender is let
in when listed in ``P_ALLOWED_USERS``, ``GATEWAY_ALLOWED_USERS``, ``P_GROUP_ALLOWED_USERS`` or the
pairing approvals, a ``"*"`` entry admitting anyone; ``TELEGRAM_GROUP_ALLOWED_CHATS`` admits every
member of those groups, and ``P_ALLOWED_ROLES`` a Discord role (members unknown). With no
allowlist at all, ``GATEWAY_ALLOW_ALL_USERS`` decides, else unknown senders are refused. Settings
made outside ``.env`` (the service's own environment, a secrets manager) aren't seen.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from openvigie.model import Channel
from openvigie.safeio import UnsafeFileError, load_json_bounded, read_text_bounded

ACCESS_NAME = re.compile(
    r"^(?:export\s+)?([A-Z][A-Z0-9_]{0,40}_(?:ALLOWED_USERS|ALLOW_ALL_USERS|GROUP_ALLOWED_CHATS|ALLOWED_ROLES))\s*="
)
TRUTHY = {"true", "1", "yes"}
ANYONE = "*"
MAX_ENTRIES = 10_000
# Hermes' platform names whose environment prefix differs from the name in capitals
PREFIX = {"qqbot": "QQ"}


@dataclass(frozen=True)
class Access:
    """Allowlists by setting name, as hashed entries; flags that are on. ``readable`` is false
    when there is no ``.env`` or it couldn't be read: then nothing is known."""

    lists: dict[str, frozenset[str]] = field(default_factory=dict)
    flags: frozenset[str] = frozenset()
    approved: dict[str, frozenset[str]] = field(default_factory=dict)  # platform → hashed ids
    readable: bool = True

    def channel(self, platform: str) -> Channel:
        if not self.readable:
            return Channel(platform)
        p = PREFIX.get(platform, platform.upper())
        users = (self.lists.get(f"{p}_ALLOWED_USERS", frozenset())
                 | self.lists.get("GATEWAY_ALLOWED_USERS", frozenset())
                 | self.lists.get(f"{p}_GROUP_ALLOWED_USERS", frozenset()))  # fmt: skip
        chats = self.lists.get(f"{p}_GROUP_ALLOWED_CHATS", frozenset())
        listed = bool(users or chats)
        users |= self.approved.get(platform, frozenset())
        anyone = (
            f"{p}_ALLOW_ALL_USERS" in self.flags
            or ANYONE in users
            or (not listed and "GATEWAY_ALLOW_ALL_USERS" in self.flags)
        )
        roles = f"{p}_ALLOWED_ROLES" in self.lists
        return Channel(
            platform=platform,
            dm_policy="open" if anyone else ("allowlist" if listed else "pairing"),
            open_dms=anyone,
            allowed_users=None if anyone or roles else len(users),
            groups=anyone or bool(chats),
        )


def read_access(home: Path) -> Access:
    path = home / ".env"
    if not path.exists():  # the settings live elsewhere (the service's environment): unknown
        return Access(readable=False)
    try:
        text = read_text_bounded(path, max_bytes=256 * 1024)
    except (OSError, UnsafeFileError, UnicodeDecodeError):
        return Access(readable=False)
    lists: dict[str, frozenset[str]] = {}
    flags: set[str] = set()
    for line in text.splitlines():
        match = ACCESS_NAME.match(line.strip())
        if match is None:
            continue  # not an access setting: its value is never looked at
        name, value = match.group(1), line.strip()[match.end() :].strip().strip("'\"")
        if name.endswith("_ALLOW_ALL_USERS"):
            if value.lower() in TRUTHY:
                flags.add(name)
        else:
            lists[name] = _hashed(_entries(value))
    return Access(lists, frozenset(flags), _approved(home))


def _entries(value: str) -> list[str]:
    """Hermes' list forms: a JSON list literal or comma-separated values."""
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed[:MAX_ENTRIES] if str(v).strip()]
    return [v.strip() for v in value.split(",")[:MAX_ENTRIES] if v.strip()]


def _hashed(entries: list[str]) -> frozenset[str]:
    return frozenset(
        e if e == ANYONE else hashlib.sha256(e.encode()).hexdigest()[:16] for e in entries
    )


def _approved(home: Path) -> dict[str, frozenset[str]]:
    found: dict[str, set[str]] = {}
    for folder in (home / "platforms" / "pairing", home / "pairing"):
        try:
            files = sorted(folder.glob("*-approved.json"))[:64] if folder.is_dir() else []
        except OSError:
            continue
        for path in files:
            try:
                data = load_json_bounded(path, max_bytes=1024 * 1024)
            except (OSError, UnsafeFileError, ValueError):
                continue
            ids = list(data)[:MAX_ENTRIES] if isinstance(data, dict | list) else []
            platform = path.name.removesuffix("-approved.json")
            found.setdefault(platform, set()).update(_hashed([str(i) for i in ids]))
    return {k: frozenset(v) for k, v in found.items()}
