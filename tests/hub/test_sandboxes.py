"""OpenShell sandboxes on the hub (FR-10): ingest, history of changes, API."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.model import AgentInstance, BlockedRequest, Host, OpenShellSandbox, to_dict
from openvigie.openshell.cli import Inventory as ShellInventory
from openvigie.openshell.cli import SandboxView
from openvigie.openshell.mapping import records
from openvigie.openshell.models import Policy, Sandbox

FIXTURES = Path(__file__).parents[1] / "openshell" / "fixtures" / "v0.1.4"
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.1.0")
OTHER = Host("h-other", "box", "linux", "aarch64", "0.1.0")
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def recorded(get: str, policy: str, agent_ids: tuple[str, ...] = ()) -> OpenShellSandbox:
    """A wire record built from recorded `sandbox get` and `policy get` outputs."""
    view = SandboxView(Sandbox.model_validate_json((FIXTURES / get).read_text()),
                       Policy.model_validate_json((FIXTURES / policy).read_text()))  # fmt: skip
    kind = f"openshell:{view.sandbox.name}"
    agents = [AgentInstance(a, PI.host_id, "claude-code", "Claude Code", sandbox_kind=kind)
              for a in agent_ids]  # fmt: skip
    (record,) = records(ShellInventory("ok", sandboxes=(view,)), PI.host_id, agents, NOW)
    return record


def test_records_carry_rules_label_and_agents() -> None:
    box = recorded("sandbox-get.json", "policy-base.json", ("a1",))
    assert (box.name, box.phase, box.agent_type, box.agent_ids) == (
        "ov-fixture", "Ready", "claude-code", ("a1",))  # fmt: skip
    assert box.network_rules == () and box.policy_version == 1
    net = recorded("net-sandbox-get.json", "net-policy-base-v2.json")
    assert net.policy_version == 3 and net.network_rules is not None
    pypi = next(r for r in net.network_rules if r.name == "pypi")
    assert [(e.host, e.ports, e.enforcement) for e in pypi.endpoints] == [
        ("pypi.org", (443,), None), ("files.pythonhosted.org", (443,), None)]  # fmt: skip


def test_store_logs_creation_policy_changes_and_deletion() -> None:
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    store = SandboxStore(db)
    v1 = recorded("net-sandbox-get.json", "net-policy-base.json")
    v3 = recorded("net-sandbox-get.json", "net-policy-base-v2.json")
    assert store.replace(PI.host_id, [v1], NOW) == 1
    assert store.replace(PI.host_id, [v1], NOW + timedelta(seconds=30)) == 0  # unchanged
    assert store.replace(PI.host_id, [v3], NOW + timedelta(minutes=1)) == 1
    assert store.replace(PI.host_id, [], NOW + timedelta(minutes=2)) == 1
    log = [(c.change, c.detail) for c in reversed(store.changes(v1.sandbox_id))]
    assert log[0] == ("created", "in workspace default")
    assert ("changed", "policy revision: 2 → 3") in log
    assert ("added", "rule ov_probe: example.com:443 read-only for /usr/bin/curl") in log
    assert log[-1] == ("deleted", "")
    assert store.all() == []


def test_a_host_cannot_overwrite_another_hosts_sandbox() -> None:
    db = Database(MEMORY)
    for host in (PI, OTHER):
        Inventory(db).upsert_host(host, NOW)
    box = recorded("sandbox-get.json", "policy-base.json")
    SandboxStore(db).replace(PI.host_id, [box], NOW)
    stolen = OpenShellSandbox(box.sandbox_id, OTHER.host_id, "stolen", NOW)
    assert SandboxStore(db).replace(OTHER.host_id, [stolen], NOW) == 0
    assert [s.name for s in SandboxStore(db).all()] == ["ov-fixture"]


def push(client: TestClient, token: str, **sections: Any) -> Any:
    body = {"schema_version": 1, "host": to_dict(PI), "sent_at": NOW.isoformat(), **sections}
    return client.post("/api/v1/ingest", json=body, headers={"authorization": f"Bearer {token}"})


def test_ingest_and_api() -> None:
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    token = Credentials(db).issue_token(PI.host_id, NOW)
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    box = to_dict(recorded("sandbox-get.json", "policy-base.json"))
    assert push(client, token, sandboxes=[box]).json()["accepted"]["sandboxes"] == 1
    assert push(client, token).json()["accepted"]["sandboxes"] == 0  # absent: not reported
    listed = client.get("/api/v1/sandboxes").json()["sandboxes"]
    assert [s["name"] for s in listed] == ["ov-fixture"]
    foreign = dict(box, host_id=OTHER.host_id)
    assert push(client, token, sandboxes=[foreign]).status_code == 403
    assert push(client, token, sandboxes=[]).json()["accepted"]["sandboxes"] == 1  # deleted
    assert client.get("/api/v1/sandboxes").json() == {"sandboxes": []}
    assert json.dumps(box)  # the wire form is plain JSON


def blocked(sandbox_id: str, binary: str, host: str, count: int, at: datetime,
            host_id: str = PI.host_id) -> BlockedRequest:  # fmt: skip
    return BlockedRequest(sandbox_id, host_id, binary, host, 443, count, at, "x")


def test_blocked_requests_add_up_until_a_rule_allows_them() -> None:
    db = Database(MEMORY)
    for host in (PI, OTHER):
        Inventory(db).upsert_host(host, NOW)
    store = SandboxStore(db)
    net = recorded("net-sandbox-get.json", "net-policy-base-v2.json")
    store.replace(PI.host_id, [net], NOW)
    later = NOW + timedelta(minutes=5)
    assert store.add_blocked(PI.host_id, [
        blocked(net.sandbox_id, "/usr/bin/bash", "example.com", 1, NOW),
        blocked(net.sandbox_id, "/usr/bin/curl", "example.com", 3, NOW),  # ov_probe allows it
        blocked(net.sandbox_id, "/usr/bin/python3.12", "pypi.org", 1, NOW),  # pypi: python3*
    ]) == 3  # fmt: skip
    assert store.add_blocked(PI.host_id, [
        blocked(net.sandbox_id, "/usr/bin/bash", "example.com", 2, later)]) == 1  # fmt: skip
    theirs = blocked(net.sandbox_id, "/usr/bin/bash", "example.com", 9, later, OTHER.host_id)
    assert store.add_blocked(OTHER.host_id, [theirs]) == 0
    (bash,) = store.still_blocked(net)
    assert (bash.binary, bash.count, bash.first_seen, bash.last_seen) == (
        "/usr/bin/bash", 3, NOW, later)  # fmt: skip
    assert len(store.blocked(net.sandbox_id)) == 3
    store.replace(PI.host_id, [], later)
    assert store.blocked(net.sandbox_id) == []


def test_ingest_refuses_another_hosts_blocked_requests() -> None:
    db = Database(MEMORY)
    Inventory(db).upsert_host(PI, NOW)
    token = Credentials(db).issue_token(PI.host_id, NOW)
    client = TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")
    box = recorded("net-sandbox-get.json", "net-policy-base-v2.json")
    push(client, token, sandboxes=[to_dict(box)])
    mine = to_dict(blocked(box.sandbox_id, "/usr/bin/bash", "example.com", 1, NOW))
    assert push(client, token, blocked=[mine]).json()["accepted"]["blocked"] == 1
    assert push(client, token, blocked=[dict(mine, host_id=OTHER.host_id)]).status_code == 403
