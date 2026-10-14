"""Applying network rules to a sandbox (FR-12, D15): checked base, Loaded, or reverted."""

import hashlib
import json
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from openvigie.collector import commands
from openvigie.collector.commands import CommandRunner
from openvigie.model import CommandStatus
from openvigie.openshell.apply import ApplyError, apply_network
from openvigie.openshell.cli import CommandError

FIXTURES = Path(__file__).parent / "fixtures" / "v0.1.4"
NEW_RULES = {"docs": {"name": "docs", "endpoints": [{"host": "docs.example.com", "port": 443}],
                      "binaries": [{"path": "/usr/bin/curl"}]}}  # fmt: skip


class FakeOpenShell:
    """Keeps policy revisions like OpenShell does; ``mode`` decides how `policy set` behaves."""

    def __init__(self, mode: str = "loads") -> None:
        recorded = json.loads((FIXTURES / "net-policy-base-v2.json").read_text())
        self.template = recorded
        self.revisions = [(3, recorded["policy"])]
        self.active = 3
        self.mode = mode
        self.calls: list[list[str]] = []
        self.files: list[str] = []

    def hash_of(self, version: int) -> str:
        body = dict(self.revisions)[version]
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    @property
    def latest(self) -> int:
        return self.revisions[-1][0]

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["policy", "get"]:
            body = dict(self.revisions)[self.latest]
            out = {**self.template, "version": self.latest, "active_version": self.active,
                   "hash": self.hash_of(self.latest), "policy": body}  # fmt: skip
            return json.dumps(out)
        assert args[:2] == ["policy", "set"] and "--wait" in args
        path = args[args.index("--policy") + 1]
        self.files.append(path)
        assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
        body = json.loads(Path(path).read_text())
        if self.mode == "rejects":
            raise CommandError("exit 1: invalid policy")
        if self.mode == "revert fails" and len(self.revisions) > 1:
            raise CommandError("exit 1: gateway unavailable")
        self.revisions.append((self.latest + 1, body))
        if self.mode in ("not loaded", "revert fails") and len(self.revisions) == 2:
            raise CommandError("timed out")
        self.active = self.latest
        return f"✓ Policy version {self.latest} loaded"


def apply(fake: FakeOpenShell, base: str | None = None, rules: Any = NEW_RULES) -> str:
    return apply_network(fake, "ov-net", "default", base or fake.hash_of(3), json.dumps(rules))


def test_rules_are_applied_and_loaded() -> None:
    fake = FakeOpenShell()
    assert apply(fake) == "policy revision 4 loaded (was 3)"
    body = dict(fake.revisions)[4]
    assert body["network_policies"] == NEW_RULES
    original = dict(fake.revisions)[3]
    assert {k: v for k, v in body.items() if k != "network_policies"} == {
        k: v for k, v in original.items() if k != "network_policies"}  # fmt: skip
    assert fake.files and not any(Path(f).exists() for f in fake.files)


def test_a_changed_base_is_refused() -> None:
    fake = FakeOpenShell()
    with pytest.raises(ApplyError, match="changed since it was reviewed"):
        apply(fake, base="0" * 64)
    assert all(c[:2] == ["policy", "get"] for c in fake.calls)


def test_a_rejected_policy_leaves_the_sandbox_unchanged() -> None:
    fake = FakeOpenShell("rejects")
    with pytest.raises(ApplyError, match="revision 3 is unchanged"):
        apply(fake)
    assert fake.latest == 3 and len(fake.files) == 1 and not Path(fake.files[0]).exists()


def test_a_revision_that_does_not_load_is_reverted() -> None:
    fake = FakeOpenShell("not loaded")
    with pytest.raises(ApplyError, match="rolled back: revision 5 restores revision 3"):
        apply(fake)
    assert dict(fake.revisions)[5] == dict(fake.revisions)[3]
    assert not any(Path(f).exists() for f in fake.files)


def test_a_failed_revert_says_so() -> None:
    fake = FakeOpenShell("revert fails")
    with pytest.raises(ApplyError, match=r"rollback failed .*check the sandbox"):
        apply(fake)


@pytest.mark.parametrize(
    ("sandbox", "rules", "message"),
    [
        ("-rf", NEW_RULES, "invalid sandbox"),
        ("ov-net", {"r": {"evil": 1}}, "invalid rules"),
        ("ov-net", [], "invalid rules"),
    ],
)
def test_bad_arguments_never_reach_openshell(sandbox: str, rules: Any, message: str) -> None:
    fake = FakeOpenShell()
    with pytest.raises(ApplyError, match=message):
        apply_network(fake, sandbox, "default", fake.hash_of(3), json.dumps(rules))
    assert fake.calls == []


def test_collector_action(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeOpenShell("rejects")
    monkeypatch.setattr(commands, "cli_runner", lambda timeout: fake)
    runner = CommandRunner(("sandbox.policy.set",))
    item = {"command_id": "b" * 32, "action": "sandbox.policy.set",
            "expires_at": "2999-01-01T00:00:00+00:00",
            "args": {"sandbox": "ov-net", "base_hash": fake.hash_of(3),
                     "network_policies": json.dumps(NEW_RULES)}}  # fmt: skip
    result = runner.run(item)
    assert result is not None and result.status is CommandStatus.FAILED
    assert result.detail.startswith("OpenShell refused the policy")
    item = {**item, "command_id": "c" * 32, "args": {"sandbox": "ov-net"}}
    result = runner.run(item)
    assert result is not None and result.detail == "missing argument base_hash"
    monkeypatch.setattr(commands, "cli_runner", lambda timeout: None)
    result = runner.run({**item, "command_id": "d" * 32})
    assert result is not None and "not installed" in result.detail
