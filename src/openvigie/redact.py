"""Secret redaction applied to everything that leaves the process.

Use ``redact`` for text, ``redact_obj`` for JSON-like structures and ``install_log_redaction``
once at startup. This is a safety net: code must still avoid reading or sending secrets at all.
Patterns are a data table so they are easy to extend; each is linear-time on bounded input.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_INPUT = 1024 * 1024
TRUNCATED = "…[TRUNCATED]"


@dataclass(frozen=True)
class Pattern:
    name: str
    regex: re.Pattern[str]
    replace: Callable[[re.Match[str]], str]


def _tag(name: str, keep: str = "") -> Callable[[re.Match[str]], str]:
    return lambda m: f"{keep}[REDACTED:{name}]"


def _keep_group(name: str) -> Callable[[re.Match[str]], str]:
    return lambda m: f"{m.group(1)}[REDACTED:{name}]"


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]{0,16384}?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----"
        ),
        _tag("private-key"),
    ),
    Pattern("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), _tag("anthropic", "sk-ant-…")),
    Pattern(
        "openai",
        re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
        _tag("openai", "sk-…"),
    ),
    Pattern(
        "github",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
        _tag("github"),
    ),
    Pattern("aws", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), _tag("aws")),
    Pattern("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), _tag("google")),
    Pattern("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), _tag("slack")),
    Pattern("telegram", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"), _tag("telegram")),
    Pattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        _tag("jwt"),
    ),
    Pattern(
        "bearer",
        re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
        _keep_group("bearer"),
    ),
    Pattern(
        "url-credentials",
        re.compile(r"\b([a-z][a-z0-9+.-]{0,20}://)[^\s/:@]{1,256}:[^\s/@]{1,256}@"),
        lambda m: f"{m.group(1)}[REDACTED:url-credentials]@",
    ),
    Pattern(
        "env-assignment",
        re.compile(
            r"(?m)^(\s{0,40}(?:export\s+)?[A-Z0-9_]{0,64}"
            r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]{0,64}\s{0,8}=\s{0,8})"
            r"(?!\S*\[REDACTED)\S+"
        ),
        _keep_group("env"),
    ),
    Pattern(
        "json-secret",
        re.compile(
            r'(?i)("(?:[a-z0-9_-]{0,32}(?:api[_-]?key|secret|password|passwd)|'
            r'(?:access|refresh|id|auth|bot|api)?[_-]?token)"\s{0,8}:\s{0,8}")'
            r'(?![^"]*\[REDACTED)[^"\\]{4,}'
        ),
        _keep_group("json"),
    ),
    Pattern(  # JSON5 / YAML / INI: `apiKey: "…"`, `botToken: '…'`, `password = …`
        "config-secret",
        re.compile(
            r"(?im)^(\s{0,40}[\"']?[a-z0-9_-]{0,32}(?:api[_-]?key|secret|password|passwd|token)"
            r"[\"']?\s{0,8}[:=]\s{0,8}[\"']?)(?![^\n]{0,8}\[REDACTED)(?!\d+(?:[\s,}\"']|$))[^\s\"',}]{4,}"
        ),
        _keep_group("config"),
    ),
)

_SENSITIVE_KEY = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|apikey|authorization|cookie|credential|"
    r"private[_-]?key)|token$"
)


def redact(text: str) -> str:
    """Return ``text`` with known secret shapes replaced by ``[REDACTED:<kind>]`` markers."""
    if len(text) > MAX_INPUT:
        text = text[:MAX_INPUT] + TRUNCATED
    for pattern in PATTERNS:
        text = pattern.regex.sub(pattern.replace, text)
    return text


def is_sensitive_key(key: str) -> bool:
    return _SENSITIVE_KEY.search(key) is not None


def redact_obj(obj: Any) -> Any:
    """Redact strings anywhere in a JSON-like value, and string values under sensitive keys."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, Mapping):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and is_sensitive_key(key) and isinstance(value, str):
                out[key] = "[REDACTED:key]" if value else value
            else:
                out[key] = redact_obj(value)
        return out
    if isinstance(obj, list | tuple):
        return type(obj)(redact_obj(v) for v in obj)
    return obj


class RedactingFilter(logging.Filter):
    """Redacts the formatted message, exception text and stack of every record it sees."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach ``RedactingFilter`` to every handler of ``logger`` (default: root).

    Filters on handlers, not loggers, so records propagated from child loggers are covered.
    Call after handlers are configured.
    """
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
