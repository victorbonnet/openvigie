import io
import logging
import time

import pytest

from openvigie.redact import (
    MAX_INPUT,
    RedactingFilter,
    install_log_redaction,
    is_sensitive_key,
    redact,
    redact_obj,
)

# Fake secrets assembled at runtime so secret scanners don't flag this file.
A = "A" * 40
SECRETS = {
    "anthropic": "sk-ant-api03-" + A,
    "openai": "sk-proj-" + A,
    "github": "ghp_" + "a1" * 18,
    "github_pat": "github_pat_" + "B" * 50,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "google": "AIza" + "x" * 35,
    "slack": "xoxb-" + "1234567890-abcdef",
    "telegram": "123456789:AA" + "h" * 33,
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
}


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_known_secret_shapes_are_redacted(kind: str) -> None:
    secret = SECRETS[kind]
    out = redact(f"before {secret} after")
    assert secret not in out
    assert "[REDACTED:" in out
    assert out.startswith("before ") and out.endswith(" after")


def test_provider_hint_kept_without_leaking_key() -> None:
    out = redact(SECRETS["anthropic"])
    assert out == "sk-ant-…[REDACTED:anthropic]"


def test_private_key_block() -> None:
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----"
    assert redact(f"x\n{block}\ny") == "x\n[REDACTED:private-key]\ny"


def test_bearer_url_env_and_json() -> None:
    value = "abcdefghijklmnopqrstuvwxyz012345"
    assert redact(f"Authorization: Bearer {value}") == "Authorization: Bearer [REDACTED:bearer]"
    assert (
        redact("postgres://user:hunter2@db:5432/x")
        == "postgres://[REDACTED:url-credentials]@db:5432/x"
    )
    env = "OPENROUTER_API_KEY=whatever123\nexport GH_TOKEN = abc\nPATH=/usr/bin"
    out = redact(env)
    assert "whatever123" not in out and "abc" not in out.splitlines()[1]
    assert "PATH=/usr/bin" in out
    assert redact('{"api_key": "plainvalue"}') == '{"api_key": "[REDACTED:json]"}'


@pytest.mark.parametrize(
    "text",
    [
        "550e8400-e29b-41d4-a716-446655440000",
        "commit 8142feff13fa7d398cc541f937b802737fe8a48c",
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk",
        "input_tokens=1234 output_tokens=56",
        "claude-sonnet-4-5-20250929",
        "/home/pi/.claude/projects/-home-pi-src/abc.jsonl",
        "task-ant-like words and sk-short",
    ],
)
def test_false_positive_guards(text: str) -> None:
    assert redact(text) == text


def test_sensitive_keys() -> None:
    for key in ["api_key", "ANTHROPIC_API_KEY", "password", "access_token", "Authorization"]:
        assert is_sensitive_key(key), key
    for key in ["input_tokens", "max_tokens", "model", "tokenizer", "session_id"]:
        assert not is_sensitive_key(key), key


def test_redact_obj_walks_structures_and_keeps_numbers() -> None:
    data = {
        "env": {"OPENAI_API_KEY": "anything", "HOME": "/home/pi"},
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "messages": ["see " + SECRETS["openai"]],
        "auth_token": "",
        "pair": ("x", SECRETS["aws"]),
    }
    out = redact_obj(data)
    assert out["env"] == {"OPENAI_API_KEY": "[REDACTED:key]", "HOME": "/home/pi"}
    assert out["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert SECRETS["openai"] not in out["messages"][0]
    assert out["auth_token"] == ""
    assert out["pair"] == ("x", "[REDACTED:aws]")


def test_log_filter_redacts_args_and_exceptions() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("openvigie.test.redact")
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    install_log_redaction(logger)
    install_log_redaction(logger)  # idempotent
    assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    try:
        logger.warning("key %s", SECRETS["anthropic"])
        try:
            raise RuntimeError(f"failed with {SECRETS['github']}")
        except RuntimeError:
            logger.exception("boom")
    finally:
        logger.removeHandler(handler)
    output = stream.getvalue()
    assert SECRETS["anthropic"] not in output
    assert SECRETS["github"] not in output
    assert "[REDACTED:anthropic]" in output and "[REDACTED:github]" in output


def test_input_is_bounded() -> None:
    out = redact("x" * (MAX_INPUT + 10))
    assert out.endswith("[TRUNCATED]")
    assert len(out) < MAX_INPUT + 20


def test_one_mebibyte_is_fast() -> None:
    chunk = "lorem ipsum sk-ant-api03-" + A + " dolor https://u:p@h/ sit amet\n"
    text = (chunk * (MAX_INPUT // len(chunk)))[:MAX_INPUT]
    start = time.perf_counter()
    redact(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"redact(1 MiB) took {elapsed:.3f}s"
