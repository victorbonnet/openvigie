import sys
from pathlib import Path

import pytest

from openvigie.openshell.versions import MIN_SUPPORTED, is_supported, newest_stable, parse

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import check_openshell


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.1.4", (0, 1, 4)),
        ("0.10.0", (0, 10, 0)),
        ("v0.2.0-pre.1", None),
        ("v0.2.0-rc1", None),
        ("latest", None),
        ("v1.2", None),
    ],
)
def test_parse(tag: str, expected: tuple[int, int, int] | None) -> None:
    assert parse(tag) == expected


def test_newest_stable_ignores_prereleases_and_sorts_numerically() -> None:
    assert newest_stable(["v0.1.4", "v0.2.0-pre.3", "v0.1.10", "v0.1.9"]) == "v0.1.10"
    assert newest_stable(["v0.2.0-pre.1"]) is None


def test_is_supported() -> None:
    assert is_supported(MIN_SUPPORTED) and is_supported("v0.1.5") and is_supported("0.2.0")
    assert not is_supported("v0.1.3") and not is_supported("v0.1.5-pre.1")


def test_dry_run_up_to_date(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(check_openshell, "release_tags", lambda: ["v0.1.4", "v0.1.5-pre.1"])
    monkeypatch.setattr(sys, "argv", ["check_openshell.py", "--dry-run"])
    assert check_openshell.main() == 0
    assert capsys.readouterr().out.startswith("up to date: OpenShell v0.1.4")


def test_newer_release_opens_one_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_call(method: str, path: str, body: object = None) -> object:
        calls.append((method, path))
        if method == "GET":
            return [{"number": 7, "title": "OpenShell v0.1.5 released: review and bump"}]
        return {"number": 8}

    monkeypatch.setattr(check_openshell, "call", fake_call)
    assert check_openshell.open_or_update("o/r", "v0.1.6") == "updated #7"
    assert check_openshell.open_or_update("o/r", "v0.1.5") == "#7 already open"
    assert calls.count(("PATCH", "/repos/o/r/issues/7")) == 1
