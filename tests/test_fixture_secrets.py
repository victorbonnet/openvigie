"""Every recorded fixture stays free of secrets: a second pass after the recorder (#55)."""

from pathlib import Path

import pytest

from openvigie.redact import redact

# hand-written fixtures holding deliberate fake secrets (ghp_FAKE…), to prove they never leak
SYNTHETIC = {
    "adapters/claude_code/fixtures/capabilities/home/.claude/settings.json",
    "adapters/claude_code/fixtures/capabilities/home/.claude.json",
}
ROOTS = [Path(__file__).parent / "adapters", Path(__file__).parent / "openshell" / "fixtures"]
FILES = sorted(p for root in ROOTS for p in root.rglob("*") if p.is_file()
               and "fixtures" in p.parts and p.suffix not in (".pyc",))  # fmt: skip


def test_there_are_fixtures_to_scan() -> None:
    assert len(FILES) > 10


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(Path(__file__).parent)))
def test_no_secret_patterns(path: Path) -> None:
    if str(path.relative_to(Path(__file__).parent)) in SYNTHETIC:
        assert "FAKE" in path.read_text()  # only ever fake values here
        return
    text = path.read_bytes().decode("utf-8", errors="replace")
    redacted = redact(text)
    if redacted != text:
        first = next(i for i, (a, b) in enumerate(zip(text, redacted, strict=False)) if a != b)
        pytest.fail(f"a secret pattern matches near character {first}; scrub this fixture")
