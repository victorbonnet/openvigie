"""Open (or update) one issue when OpenShell has a stable release newer than MIN_SUPPORTED.

Run weekly by .github/workflows/openshell-release.yml. ``--dry-run`` only prints the result.
Stdlib only; needs GITHUB_TOKEN and GITHUB_REPOSITORY unless ``--dry-run``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from openvigie.openshell.versions import MIN_SUPPORTED, newest_stable, parse

API = "https://api.github.com"
UPSTREAM = "NVIDIA/OpenShell"
TITLE_PREFIX = "OpenShell "
TITLE_SUFFIX = " released: review and bump"


def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    headers = {"accept": "application/vnd.github+json", "user-agent": "openvigie-ci"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API + path, data, headers, method=method)  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https URL
        return json.loads(response.read() or b"null")


def release_tags() -> list[str]:
    releases = call("GET", f"/repos/{UPSTREAM}/releases?per_page=50")
    return [r["tag_name"] for r in releases if not r.get("draft") and not r.get("prerelease")]


def issue_body(tag: str) -> str:
    return (
        f"[OpenShell {tag}](https://github.com/{UPSTREAM}/releases/tag/{tag}) is out; openvigie "
        f"supports {MIN_SUPPORTED} and newer.\n\n"
        "- [ ] Read the release notes for CLI, policy-format and sandbox-listing changes\n"
        "- [ ] Re-record the OpenShell fixtures if outputs changed\n"
        "- [ ] Bump `MIN_SUPPORTED` in `src/openvigie/openshell/versions.py` and "
        "`docs/sandboxing.md`\n"
    )


def open_or_update(repo: str, tag: str) -> str:
    title = f"{TITLE_PREFIX}{tag}{TITLE_SUFFIX}"
    for issue in call("GET", f"/repos/{repo}/issues?state=open&per_page=100"):
        if issue["title"].startswith(TITLE_PREFIX) and issue["title"].endswith(TITLE_SUFFIX):
            if issue["title"] != title:
                call("PATCH", f"/repos/{repo}/issues/{issue['number']}",
                     {"title": title, "body": issue_body(tag)})  # fmt: skip
                return f"updated #{issue['number']}"
            return f"#{issue['number']} already open"
    created = call("POST", f"/repos/{repo}/issues", {"title": title, "body": issue_body(tag)})
    return f"opened #{created['number']}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print, don't touch issues")
    args = parser.parse_args()
    newest = newest_stable(release_tags())
    newest_v, supported_v = parse(newest or ""), parse(MIN_SUPPORTED)
    if newest is None or newest_v is None or supported_v is None or newest_v <= supported_v:
        print(f"up to date: OpenShell {newest or '?'}, supported from {MIN_SUPPORTED}")
        return 0
    print(f"newer OpenShell: {newest} (supported from {MIN_SUPPORTED})")
    if not args.dry_run:
        print(open_or_update(os.environ["GITHUB_REPOSITORY"], newest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
