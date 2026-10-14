"""The recorded OpenShell fixtures stay usable: valid JSON, scrubbed, for the supported version."""

import json
from pathlib import Path

from openvigie.openshell.versions import MIN_SUPPORTED

FIXTURES = Path(__file__).parent / "fixtures" / f"v{MIN_SUPPORTED}"


def body(name: str) -> str:
    """The stdout part of a recorded command (create commands print a progress line first)."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return text[text.index("{") :] if name.startswith("create") else text


def test_fixtures_exist_for_the_supported_version() -> None:
    assert (FIXTURES / "version.txt").read_text().split()[-1] == MIN_SUPPORTED


def test_json_fixtures_parse_and_are_scrubbed() -> None:
    names = [p.name for p in FIXTURES.glob("*.json") if not p.read_text().startswith("# exit")]
    assert "sandbox-get.json" in names and "net-policy-base-v2.json" in names
    for name in names:
        if name == "docker-ps.json":
            continue  # one JSON object per line
        data = json.loads(body(name))
        assert isinstance(data, dict), name
    for path in FIXTURES.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "SHA256:FIXTURE-HOST-KEY" in text or "SHA256:" not in text, path.name
