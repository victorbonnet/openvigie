"""Which OpenShell versions openvigie supports: the latest stable release and newer."""

from __future__ import annotations

import re

MIN_SUPPORTED = "0.1.4"

_STABLE = re.compile(r"^v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})$")


def parse(tag: str) -> tuple[int, int, int] | None:
    """``"v0.1.4"`` → ``(0, 1, 4)``; ``None`` for pre-releases (``-pre.N``, ``-rc1``) and junk."""
    match = _STABLE.match(tag.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def is_supported(version: str) -> bool:
    """True for a stable version at or above ``MIN_SUPPORTED``."""
    parsed, minimum = parse(version), parse(MIN_SUPPORTED)
    return parsed is not None and minimum is not None and parsed >= minimum


def newest_stable(tags: list[str]) -> str | None:
    """The highest stable tag, ignoring pre-releases."""
    stable = [(v, tag) for tag in tags if (v := parse(tag)) is not None]
    return max(stable)[1] if stable else None
