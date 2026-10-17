"""Every recorded fixture stays free of secrets: a second pass after the recorder (#55)."""

from pathlib import Path

import pytest

from openvigie.redact import MAX_INPUT, redact

# hand-written fixtures holding deliberate fake secrets (ghp_FAKE…), to prove they never leak
SYNTHETIC = {
    "adapters/claude_code/fixtures/capabilities/home/.claude/settings.json",
    "adapters/claude_code/fixtures/capabilities/home/.claude.json",
}
ROOTS = [Path(__file__).parent / "adapters", Path(__file__).parent / "openshell" / "fixtures"]
FILES = sorted(p for root in ROOTS for p in root.rglob("*") if p.is_file()
               and "fixtures" in p.parts and p.suffix not in (".pyc",))  # fmt: skip


def secret_found(text: str) -> str | None:
    # a key block spans lines, so the line-by-line pass below can't see it
    if "PRIVATE KEY-----" in text:
        return "a private key block"
    # line by line: redact() truncates past MAX_INPUT, so a whole large file would always differ
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > MAX_INPUT:
            return f"line {number}, too long to scan; record a smaller fixture"
        redacted = redact(line)
        if redacted != line:
            first = next(i for i, (a, b) in enumerate(zip(line, redacted, strict=False)) if a != b)
            return f"a secret pattern on line {number} near character {first}"
    return None


def test_there_are_fixtures_to_scan() -> None:
    assert len(FILES) > 10


def test_private_key_block_found() -> None:
    block = (
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAA\n-----END OPENSSH " + "PRIVATE KEY-----\n"
    )
    assert secret_found('{"a": 1}\n' + block) == "a private key block"
    assert secret_found('{"a": 1}\n{"b": 2}\n') is None


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(Path(__file__).parent)))
def test_no_secret_patterns(path: Path) -> None:
    if str(path.relative_to(Path(__file__).parent)) in SYNTHETIC:
        assert "FAKE" in path.read_text()  # only ever fake values here
        return
    found = secret_found(path.read_bytes().decode("utf-8", errors="replace"))
    if found:
        pytest.fail(f"{found}; scrub this fixture")
