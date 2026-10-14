"""Check `sandbox.policy.set` against a real OpenShell (FR-12, D15); run in CI.

Creates a sandbox from the Claude Code starter policy, then, through openvigie's own code:
1. applies network rules with one extra rule: the new revision must be Loaded and contain it;
2. applies a stale base hash: refused, nothing changes;
3. applies rules OpenShell rejects: the error is reported and the rules are still those of step 1.
Exits 1 on the first failure. The sandbox is deleted at the end.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

from openvigie.openshell.apply import WAIT_S, ApplyError, apply_network
from openvigie.openshell.cli import cli_runner
from openvigie.openshell.models import Policy
from openvigie.openshell.policy import parse_yaml, to_json

NAME = "ovs-apply"
STARTER = Path("tests/openshell/starter/claude-code.yaml")
EXTRA = {"name": "ov_extra", "endpoints": [{"host": "docs.example.com", "port": 443}],
         "binaries": [{"path": "/usr/bin/curl"}]}  # fmt: skip
REJECTED = {"name": "ov_bad", "endpoints": [{"host": "*.com", "port": 443}],
            "binaries": [{"path": "/usr/bin/curl"}]}  # fmt: skip


def fail(message: str) -> NoReturn:
    print(f"FAIL: {message}")
    sys.exit(1)


def main() -> None:
    run = cli_runner(timeout=2 * WAIT_S + 30)
    if run is None:
        fail("openshell is not installed")
    run(["sandbox", "create", "--name", NAME, "--policy", str(STARTER), "--detach",
         "--output", "json", "--", "sleep", "900"])  # fmt: skip
    for _ in range(60):
        if '"Ready"' in run(["sandbox", "get", NAME, "--output", "json"]):
            break
        time.sleep(5)
    else:
        fail("the sandbox never became Ready")

    def current() -> Policy:
        return Policy.model_validate_json(run(["policy", "get", NAME, "--base", "-o", "json"]))

    start = current()
    rules = json.loads(to_json(parse_yaml(STARTER.read_text()).rules))
    print("1.", apply_network(run, NAME, "default", start.hash, json.dumps({**rules,
          "ov_extra": EXTRA})))  # fmt: skip
    after = current()
    if "ov_extra" not in after.policy.network_policies or after.active_version != after.version:
        fail(f"the new rule is not loaded: {after}")
    try:
        apply_network(run, NAME, "default", start.hash, json.dumps(rules))
        fail("a stale base hash was accepted")
    except ApplyError as exc:
        print("2.", exc)
    try:
        apply_network(run, NAME, "default", after.hash, json.dumps({**rules, "ov_bad": REJECTED}))
        print("3. OpenShell accepted *.com; nothing to revert (no failure to test)")
    except ApplyError as exc:
        print("3.", exc)
    final = current()
    if "ov_extra" not in final.policy.network_policies or "ov_bad" in final.policy.network_policies:
        fail(f"the rules after a refused change are not those of step 1: {final}")
    print("ok: applied, Loaded, stale base refused, refused change left the sandbox as it was")


if __name__ == "__main__":
    try:
        main()
    finally:
        subprocess.run(["openshell", "sandbox", "delete", NAME], check=False,  # noqa: S603, S607
                       capture_output=True)  # fmt: skip
