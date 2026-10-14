"""Apply network rules to a running sandbox (FR-12, PRD D15): openvigie's only write to OpenShell.

Offers ``apply_network(run, sandbox, workspace, base_hash, rules_json)``, which returns a line for
the hub or raises ``ApplyError``. Only ``network_policies`` changes: filesystem, process and
landlock stay as they are, since OpenShell applies those to new sandboxes only.

1. Read the sandbox's base policy; refuse if its hash isn't the one the admin reviewed.
2. Write the merged policy as JSON (valid YAML) to a private temp file and run
   ``openshell policy set --wait``, which hot-reloads the network rules.
3. Read the policy back: the new revision must be the active (Loaded) one.
4. Otherwise, if anything changed, set the previous policy again (automatic revert, D15).

Like every OpenShell call here, it runs an argv list (no shell) with bounded output, and names
are checked before use.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from openvigie.openshell.cli import SAFE_NAME, CommandError, Run
from openvigie.openshell.models import Policy
from openvigie.openshell.policy import PolicyError, from_json, to_json

WAIT_S = 60  # `policy set --wait --timeout`; the runner's own timeout must be longer


class ApplyError(Exception):
    """The rules were not applied; the message says why and what state the sandbox is in."""


def apply_network(run: Run, sandbox: str, workspace: str, base_hash: str, rules_json: str) -> str:
    if not (SAFE_NAME.match(sandbox) and SAFE_NAME.match(workspace)):
        raise ApplyError("invalid sandbox or workspace name")
    try:
        rules = from_json(rules_json)
    except PolicyError as exc:
        raise ApplyError(f"invalid rules: {exc}") from None
    before, raw = _read(run, sandbox, workspace)
    if before.hash != base_hash:
        raise ApplyError("the policy changed since it was reviewed; review it again")
    merged = {**raw, "network_policies": json.loads(to_json(rules))}
    try:
        _set(run, sandbox, workspace, merged)
        after, _ = _read(run, sandbox, workspace)
        if after.active_version != after.version or after.version == before.version:
            raise CommandError(f"revision {after.version} is not loaded")
    except (CommandError, ApplyError) as exc:
        raise ApplyError(_revert(run, sandbox, workspace, before, raw, str(exc))) from None
    return f"policy revision {after.version} loaded (was {before.version})"


def _revert(run: Run, sandbox: str, workspace: str, before: Policy, raw: dict[str, Any],
            why: str) -> str:  # fmt: skip
    try:
        now, _ = _read(run, sandbox, workspace)
        if now.hash == before.hash:
            return f"OpenShell refused the policy ({why}); revision {before.version} is unchanged"
        _set(run, sandbox, workspace, raw)
        restored, _ = _read(run, sandbox, workspace)
    except (CommandError, ApplyError) as exc:
        return f"OpenShell refused the policy ({why}); rollback failed ({exc}): check the sandbox"
    return (f"OpenShell refused the policy ({why}); rolled back: revision {restored.version} "
            f"restores revision {before.version}")  # fmt: skip


def _read(run: Run, sandbox: str, workspace: str) -> tuple[Policy, dict[str, Any]]:
    """The base policy: the typed view, and the policy body exactly as OpenShell sent it."""
    output = run(["policy", "get", sandbox, "--workspace", workspace, "--base", "-o", "json"])
    try:
        policy = Policy.model_validate_json(output)
        body = json.loads(output).get("policy", {})
    except (ValidationError, ValueError, AttributeError):
        raise ApplyError("unexpected `policy get` output") from None
    if not isinstance(body, dict) or not policy.hash:
        raise ApplyError("unexpected `policy get` output")
    return policy, body


def _set(run: Run, sandbox: str, workspace: str, policy: dict[str, Any]) -> None:
    fd, path = tempfile.mkstemp(prefix="openvigie-policy-", suffix=".yaml")  # mode 0600
    try:
        os.write(fd, json.dumps(policy, indent=1).encode())
        os.close(fd)
        fd = -1
        run(["policy", "set", sandbox, "--workspace", workspace, "--policy", path, "--wait",
             "--timeout", str(WAIT_S)])  # fmt: skip
    finally:
        if fd >= 0:
            os.close(fd)
        Path(path).unlink()
