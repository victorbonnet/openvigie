#!/usr/bin/env python3
"""Release checks, run by .github/workflows/release.yml before anything is published.

python scripts/check_release.py version 0.1.0     # tag ↔ pyproject.toml ↔ CHANGELOG.md
python scripts/check_release.py wheel dist/openvigie-0.1.0-py3-none-any.whl
"""

from __future__ import annotations

import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# files the hub and collector need at run time; a wheel without them installs but breaks
REQUIRED = (
    "openvigie/prices.toml",
    "openvigie/risk.toml",
    "openvigie/hub/static/app.css",
    "openvigie/hub/static/vendor/htmx-2.0.4.min.js",
    "openvigie/hub/static/vendor/htmx-LICENSE.txt",
    "openvigie/hub/store/migrations/0001_inventory.sql",
    "openvigie/hub/store/migrations/0006_openshell.sql",
    "openvigie/hub/store/migrations/0007_audit.sql",
    "openvigie/hub/store/migrations/0008_commands.sql",
    "openvigie/hub/store/migrations/0009_actions.sql",
    "openvigie/hub/store/migrations/0010_alerts.sql",
    "openvigie/openshell/starter.toml",
    "openvigie/hub/templates/base.html",
)
FORBIDDEN_PREFIXES = ("scripts/", "tests/", "openvigie/tests/")


def check_version(tag_version: str, root: Path = ROOT) -> list[str]:
    errors = []
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if project["version"] != tag_version:
        errors.append(f"tag says {tag_version}, pyproject.toml says {project['version']}")
    changelog = (root / "CHANGELOG.md").read_text()
    if f"## [{tag_version}]" not in changelog:
        errors.append(f"CHANGELOG.md has no '## [{tag_version}]' section")
    return errors


def check_wheel(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as wheel:
        names = set(wheel.namelist())
    errors = [f"missing {name}" for name in REQUIRED if name not in names]
    if not any(n.endswith(".dist-info/licenses/LICENSE") for n in names):
        errors.append("missing the license file in .dist-info/licenses/")
    migrations = [n for n in names if n.startswith("openvigie/hub/store/migrations/")]
    if not migrations:
        errors.append("no migrations")
    errors += [f"must not ship {n}" for n in sorted(names) if n.startswith(FORBIDDEN_PREFIXES)]
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("version", "wheel"):
        print(__doc__, file=sys.stderr)
        return 2
    errors = check_version(argv[1]) if argv[0] == "version" else check_wheel(Path(argv[1]))
    for error in errors:
        print(f"release check: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
