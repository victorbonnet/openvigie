"""Network rules written by an admin (FR-12, D15): parse, check, round-trip, compare."""

import json
from pathlib import Path

import pytest

from openvigie.openshell.policy import (
    MAX_TEXT,
    PolicyError,
    allowing,
    diff,
    from_json,
    parse_yaml,
    to_json,
    to_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "v0.1.4"
STARTERS = Path(__file__).parent / "starter"
SECRET = "sk-ant-api03-" + "q" * 30


def recorded() -> list[dict[str, object]]:
    out = []
    for path in sorted(FIXTURES.glob("*policy*.json")):
        data = json.loads(path.read_text())
        out.append(data.get("policy", data).get("network_policies", {}))
    return out


@pytest.mark.parametrize("raw", recorded())
def test_recorded_policies_round_trip_exactly(raw: dict[str, object]) -> None:
    rules = from_json(json.dumps(raw))
    assert json.loads(to_json(rules)) == raw  # what goes back to OpenShell is what came out
    assert parse_yaml(to_yaml(rules)).rules == rules


@pytest.mark.parametrize("name", ["claude-code.yaml", "codex.yaml"])
def test_starter_policies_parse(name: str) -> None:
    parsed = parse_yaml((STARTERS / name).read_text())
    assert parsed.rules and parsed.ignored == ("version", "filesystem_policy", "landlock")
    assert all(rule.name == key for key, rule in parsed.rules.items())
    assert parse_yaml(to_yaml(parsed.rules)).rules == parsed.rules


def test_empty_rules() -> None:
    assert parse_yaml("network_policies: {}").rules == {}
    assert to_yaml({}) == "network_policies: {}\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("a: &x [1, 2]\nb: *x\nnetwork_policies: {}", "line 1: YAML anchors and aliases"),
        ("network_policies: " + "[" * 3000, "not valid YAML"),
        ("x: 'open", "line 1: not valid YAML"),
        ("- 1", "expected a mapping"),
        ("version: 1", "no network_policies section"),
        ("other: 1\nnetwork_policies: {}", "unknown section"),
        ("network_policies: {r: {evil: 1}}", "network_policies.r.evil: not a field"),
        ("network_policies: {r: {endpoints: [{host: a.b, port: 0}]}}", "endpoints.0.port"),
        ("network_policies: {r: {endpoints: [{port: 443}]}}", "a host or allowed_ips"),
        ("network_policies: {r: {endpoints: [{host: a.b}]}}", "a port or ports"),
        ("network_policies: {r: {binaries: [{path: curl}]}}", "binaries.0.path"),
        ("network_policies: {r: {name: other}}", "name must match the key"),
        ("network_policies: {'bad key': {}}", "network_policies.bad key"),
        ("network_policies: {r: {endpoints: [{host: a.b, port: 443, access: everything}]}}",
         "access"),
    ],
)  # fmt: skip
def test_invalid_text_is_refused(text: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_yaml(text)


def test_errors_never_quote_values() -> None:
    texts = [
        f"network_policies: {{r: {{endpoints: [{{host: '{SECRET} x', port: 443}}]}}}}",
        f"network_policies: {{r: {{binaries: [{{path: '{SECRET}'}}]}}}}",
        f"network_policies: '{SECRET}'",
        f"'{SECRET}",
    ]
    for text in texts:
        with pytest.raises(PolicyError) as exc:
            parse_yaml(text)
        assert SECRET not in str(exc.value)


def test_size_and_alias_bombs() -> None:
    with pytest.raises(PolicyError, match="KiB"):
        parse_yaml("#" * (MAX_TEXT + 1))
    laughs = "a: &a [x, x]\n" + "".join(f"{c}: &{c} [*{p}, *{p}]\n" for p, c in
                                         zip("abcdefgh", "bcdefghi", strict=True))  # fmt: skip
    with pytest.raises(PolicyError, match="aliases"):
        parse_yaml(laughs + "network_policies: {}")


def test_diff() -> None:
    old = parse_yaml("""
network_policies:
  github:
    endpoints: [{host: api.github.com, port: 443, access: read-only, enforcement: enforce}]
    binaries: [{path: /usr/bin/curl}]
  pypi:
    endpoints: [{host: pypi.org, port: 443}]
    binaries: [{path: /usr/bin/pip}]
""").rules
    new = parse_yaml("""
network_policies:
  github:
    endpoints: [{host: api.github.com, port: 443, access: read-write, enforcement: enforce}]
    binaries: [{path: /usr/bin/curl}]
  npm:
    endpoints: [{host: registry.npmjs.org, ports: [443, 8443]}]
    binaries: [{path: /usr/bin/npm}]
""").rules
    changes = {c.rule: c for c in diff(old, new)}
    assert {k: c.change for k, c in changes.items()} == {
        "github": "changed", "npm": "added", "pypi": "removed"}  # fmt: skip
    assert changes["github"].details == ("- api.github.com:443 read-only",
                                         "+ api.github.com:443 read-write")  # fmt: skip
    assert changes["npm"].details == ("registry.npmjs.org:443/8443 (audit only)",
                                      "binary /usr/bin/npm")  # fmt: skip
    assert diff(old, old) == []


def test_allowing_adds_one_rule_for_the_blocked_program() -> None:
    rules = from_json(json.dumps({"allow_example_com_443": {
        "endpoints": [{"host": "example.com", "port": 443}],
        "binaries": [{"path": "/usr/bin/curl"}]}}))  # fmt: skip
    added = allowing(rules, "/usr/bin/bash", "example.com", 443)
    assert set(added) == {"allow_example_com_443", "allow_example_com_443_2"}
    (change,) = diff(rules, added)
    assert change.change == "added" and change.rule == "allow_example_com_443_2"
    assert '"/usr/bin/bash"' in to_yaml(added) and parse_yaml(to_yaml(added)).rules == added
    assert set(allowing({}, "/usr/bin/bash", "1.1.1.1", 443)) == {"allow_1_1_1_1_443"}


def test_allowing_refuses_what_the_editor_cannot_write() -> None:
    with pytest.raises(PolicyError):
        allowing({}, "/usr/bin/bash", "[2001:db8::1]", 443)
