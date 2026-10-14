"""The OpenShell reader, against outputs recorded from OpenShell v0.1.4."""

import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from openvigie.openshell import cli
from openvigie.openshell.cli import CommandError, Reader, cli_runner

FIXTURES = Path(__file__).parent / "fixtures" / "v0.1.4"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def page_of(*gets: str) -> str:
    """A `sandbox list` page made of recorded `sandbox get` outputs."""
    return json.dumps({"sandboxes": [json.loads(fixture(g)) for g in gets], "next_page_token": ""})


class FakeCli:
    """Answers the commands the reader may run from fixtures, and records every call."""

    def __init__(self, listing: str, version: str = "openshell 0.1.4\n") -> None:
        self.listing, self.version = listing, version
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(tuple(args))
        match list(args):
            case ["--version"]:
                return self.version
            case ["sandbox", "list", *_]:
                return self.listing
            case ["sandbox", "get", "ov-fixture", "--workspace", "default", "-o", "json"]:
                return fixture("sandbox-get.json")
            case ["sandbox", "get", "ov-net", "--workspace", "default", "-o", "json"]:
                return fixture("net-sandbox-get.json")
            case ["policy", "get", "ov-fixture", "--workspace", "default", "--base", "-o", "json"]:
                return fixture("policy-base.json")
            case ["policy", "get", "ov-net", "--workspace", "default", "--base", "-o", "json"]:
                return fixture("net-policy-base-v2.json")
        raise AssertionError(f"unexpected command {args}")


def test_reads_a_labelled_sandbox_with_the_default_policy() -> None:
    inventory = Reader(FakeCli(fixture("sandbox-list-all.json"))).read()
    assert inventory.state == "ok" and inventory.version == "0.1.4"
    (view,) = inventory.sandboxes
    assert view.sandbox.name == "ov-fixture" and view.sandbox.phase == "Ready"
    assert view.sandbox.labels == {"openvigie.agent": "claude-code"}
    assert view.sandbox.main_process_started_at_ms is not None  # only `sandbox get` has it
    assert view.policy is not None and view.policy.policy.network_policies == {}
    assert view.policy.hash == view.sandbox.configuration_admission.policy_hash  # type: ignore[union-attr]


def test_reads_network_rules() -> None:
    (view,) = Reader(FakeCli(page_of("net-sandbox-get.json"))).read().sandboxes
    assert view.policy is not None and view.policy.version == 3
    rules = view.policy.policy.network_policies
    assert sorted(rules) == ["github_readonly", "ov_probe", "pypi"]
    github = rules["github_readonly"].endpoints[0]
    assert (github.host, github.port, github.access, github.enforcement) == (
        "api.github.com", 443, "read-only", "enforce")  # fmt: skip
    assert [e.host for e in rules["pypi"].endpoints] == ["pypi.org", "files.pythonhosted.org"]
    assert rules["pypi"].endpoints[0].enforcement is None  # OpenShell's default: audit
    assert rules["pypi"].binaries[0].path == "/usr/bin/python3*"
    raw = json.loads(fixture("net-policy-base-v2.json"))["policy"]["network_policies"]
    assert view.policy.policy.raw_network is not None
    assert json.loads(view.policy.policy.raw_network) == raw  # exact, for editing (FR-12)


def test_details_and_policies_are_fetched_again_only_when_they_change() -> None:
    fake = FakeCli(fixture("sandbox-list-all.json"))
    reader = Reader(fake)
    reader.read()
    first = len(fake.calls)
    reader.read()
    assert len(fake.calls) - first == 2  # --version and sandbox list only
    listing = json.loads(fake.listing)
    listing["sandboxes"][0]["resource_version"] += 1
    fake.listing = json.dumps(listing)
    reader.read()
    assert fake.calls[-1][:3] == ("sandbox", "get", "ov-fixture")  # policy hash unchanged


def test_not_installed_unsupported_and_unreachable() -> None:
    assert Reader(None).read().state == "not_installed"
    old = Reader(FakeCli("", version="openshell 0.1.3\n")).read()
    assert old.state == "unsupported" and "0.1.4 or newer" in (old.reason or "")

    def down(args: Sequence[str]) -> str:
        if list(args) == ["--version"]:
            return "openshell 0.1.4\n"
        raise CommandError("exit 1: client error (Connect)")

    unreachable = Reader(down).read()
    assert unreachable.state == "unreachable" and "Connect" in (unreachable.reason or "")


def test_names_that_could_be_flags_are_never_passed_on() -> None:
    listing = json.loads(fixture("sandbox-list-all.json"))
    listing["sandboxes"][0]["name"] = "--policy=/etc/shadow"
    fake = FakeCli(json.dumps(listing))
    assert Reader(fake).read().sandboxes == ()
    assert all(call[:2] != ("sandbox", "get") for call in fake.calls)


def test_garbage_listing_is_an_error_not_a_crash() -> None:
    assert Reader(FakeCli("not json")).read().state == "error"


# --- the real subprocess path, with a stand-in `openshell` --------------------------------------


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    script = tmp_path / "openshell"
    script.write_text(f"""#!{sys.executable}
import os, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("openshell 0.1.4")
elif args == ["env"]:
    print(" ".join(sorted(os.environ)))
elif args == ["down"]:
    sys.stderr.write({fixture("status-gateway-down.txt").split(chr(10), 1)[1]!r})
    sys.exit(1)
elif args == ["slow"]:
    time.sleep(10)
elif args == ["huge"]:
    sys.stdout.write("x" * (8 * 1024 * 1024))
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_cli_runner_runs_the_binary_with_a_minimal_environment(
    fake_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-for-openshell")
    run = cli_runner(str(fake_binary))
    assert run is not None and run(["--version"]).strip() == "openshell 0.1.4"
    names = set(run(["env"]).split())
    added_by_os = {n for n in names if n.startswith(("__CF_", "LC_"))} | {"PWD"}  # macOS, libc
    assert "ANTHROPIC_API_KEY" not in names
    assert names - added_by_os <= {*cli.ENV_KEEP, "NO_COLOR"}


def test_cli_runner_failures_are_bounded_errors(fake_binary: Path) -> None:
    run = cli_runner(str(fake_binary), timeout=1.0)
    assert run is not None
    with pytest.raises(CommandError, match=r"exit 1: client error \(Connect\)"):
        run(["down"])
    with pytest.raises(CommandError, match="timed out"):
        run(["slow"])
    with pytest.raises(CommandError, match="output too large"):
        run(["huge"])


def test_no_binary_means_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.devnull)
    assert cli_runner() is None
