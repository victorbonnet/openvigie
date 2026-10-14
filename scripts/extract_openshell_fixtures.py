"""Split a recorder log into fixture files: tests/openshell/fixtures/v<version>/<name>.

Usage: python scripts/extract_openshell_fixtures.py <job-log.txt> [--out DIR]
The log is the output of scripts/record_openshell.sh (a CI job log is fine: timestamps are
stripped). Values that identify the recording machine are replaced by fixed placeholders.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+Z ")
SECTION = re.compile(r"^=== ([\w.-]+) ===$")
EXIT = re.compile(r"^--- exit (\d+) ---$")
SCRUB = [
    (re.compile(r"SHA256:[A-Za-z0-9+/=]{20,}"), "SHA256:FIXTURE-HOST-KEY"),
    (re.compile(r'("internal\.openshell\.ai/gateway-token-id": ")[^"]+'), r"\1fixture-token-id"),
]


def sections(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    name: str | None = None
    body: list[str] = []
    for line in lines:
        line = TIMESTAMP.sub("", line)
        if m := SECTION.match(line):
            name, body = m[1], []
        elif name and (m := EXIT.match(line)):
            text = "\n".join(body).strip("\n") + "\n"
            out[name] = text if m[1] == "0" else f"# exit {m[1]}\n{text}"
            name = None
        elif name:
            body.append(line)
    return out


def scrub(text: str) -> str:
    for pattern, replacement in SCRUB:
        text = pattern.sub(replacement, text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    found = sections(args.log.read_text(encoding="utf-8").splitlines())
    version = found.get("version.txt", "").split()[-1] if found.get("version.txt") else "unknown"
    out = args.out or Path(__file__).parents[1] / "tests" / "openshell" / "fixtures" / f"v{version}"
    out.mkdir(parents=True, exist_ok=True)
    for name, text in sorted(found.items()):
        if name.startswith("docker-inspect-"):
            continue  # the collector never talks to Docker; docker-ps.json is kept for reference
        (out / name).write_text(scrub(text), encoding="utf-8")
    print(f"{len(found)} files in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
