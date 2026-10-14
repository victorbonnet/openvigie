import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import check_release


def make_wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as wheel:
        for name in names:
            wheel.writestr(name, "x")
    return path


def test_wheel_check(tmp_path: Path) -> None:
    good = [*check_release.REQUIRED, "openvigie-0.1.0.dist-info/licenses/LICENSE"]
    assert check_release.check_wheel(make_wheel(tmp_path / "ok.whl", good)) == []
    bad = make_wheel(tmp_path / "bad.whl", [*good[1:], "tests/test_x.py"])
    errors = check_release.check_wheel(bad)
    assert "missing openvigie/prices.toml" in errors and "must not ship tests/test_x.py" in errors


def test_version_check(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (tmp_path / "CHANGELOG.md").write_text("## [0.1.0] - 2026-10-20\n")
    assert check_release.check_version("0.1.0", tmp_path) == []
    assert len(check_release.check_version("0.2.0", tmp_path)) == 2


def test_repository_changelog_has_the_current_version() -> None:
    root = Path(__file__).parents[1]
    assert "## [0.1.0]" in (root / "CHANGELOG.md").read_text()
