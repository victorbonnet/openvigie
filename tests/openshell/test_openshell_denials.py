from collections.abc import Sequence
from pathlib import Path

import pytest

from openvigie.openshell.cli import CommandError, Reader, SandboxView
from openvigie.openshell.denials import Denial, Tally, parse
from openvigie.openshell.models import Sandbox
from openvigie.openshell.policy import PolicyError, allowing

FIXTURES = Path(__file__).parent / "fixtures" / "v0.1.4"
LOG = (FIXTURES / "net-logs.txt").read_text()
BASH = "/usr/bin/bash"


def test_recorded_denials() -> None:
    found = parse(LOG)
    assert [(d.binary, d.host, d.port) for d in found] == [
        (BASH, "www.wikipedia.org", 443),
        (BASH, "www.wikipedia.org", 443),
        (BASH, "example.com", 443),  # allowed for curl only
        (BASH, "1.1.1.1", 443),
    ]
    assert {d.reason for d in found} == {"transparent_tcp_policy_denied"}
    assert found[0].at == 1792237770.937


def test_other_lines_are_ignored() -> None:
    assert parse((FIXTURES / "net-logs-gateway.txt").read_text()) == []
    proxy_warning = next(line for line in LOG.splitlines() if "cmdline" in line)
    assert parse(proxy_warning) == []
    odd = [
        "[1.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED bash(1) -> a.example:443 [reason:x]",
        "[1.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /b(1) -> a.example:99999 [reason:x]",
        "[1.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /b(1) -> a/b:443 [reason:x]",
        "[1.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] 127.0.0.1:3128",
    ]
    assert parse("\n".join(odd)) == []


def test_tally_counts_each_line_once() -> None:
    tally = Tally()
    tally.add("s1", parse(LOG))
    groups = tally.pending["s1"]
    assert groups[(BASH, "www.wikipedia.org", 443)].count == 2
    assert groups[(BASH, "example.com", 443)].count == 1
    tally.pending = {}
    tally.add("s1", parse(LOG))  # the same log read again
    assert tally.pending == {}
    tally.add("s1", [Denial(1792237999.0, BASH, "example.com", 443, "x")])
    assert tally.pending["s1"][(BASH, "example.com", 443)].count == 1
    tally.forget(set())
    assert tally.seen == {} and tally.pending == {}


def view(name: str = "ov-net", workspace: str = "team") -> SandboxView:
    return SandboxView(Sandbox(id="s1", name=name, workspace=workspace), None)


def test_reader_runs_logs_for_one_sandbox() -> None:
    calls: list[tuple[str, ...]] = []

    def run(args: Sequence[str]) -> str:
        calls.append(tuple(args))
        return LOG

    assert len(Reader(run).denials(view()) or []) == 4
    assert calls == [("logs", "ov-net", "--workspace", "team", "-n", "500", "--source", "sandbox")]


def test_unreadable_log_is_none() -> None:
    def run(args: Sequence[str]) -> str:
        raise CommandError("exit 1: sandbox not found")

    assert Reader(run).denials(view()) is None
    assert Reader(None).denials(view()) is None


def denial_to(host: str) -> str:
    return (f"[5.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/bin/bash(1) -> {host}:443 "
            "[reason:transparent_tcp_policy_denied]")  # fmt: skip


def test_glob_destinations_are_not_parsed() -> None:
    assert parse("\n".join(denial_to(h) for h in ("*", "*.example.com", "a?.example"))) == []
    assert len(parse(denial_to("a.example.com"))) == 1


def test_a_changed_log_format_reads_as_unknown() -> None:
    changed = denial_to("a.example.com").replace("[reason:", "{reason=")

    def run(args: Sequence[str]) -> str:
        return changed

    assert Reader(run).denials(view()) is None
    quiet = "[5.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] 1.2.3.4:1\n"
    assert Reader(lambda args: quiet).denials(view()) == []


def test_allowing_refuses_globs() -> None:
    with pytest.raises(PolicyError):
        allowing({}, "/usr/bin/bash", "*", 443)
