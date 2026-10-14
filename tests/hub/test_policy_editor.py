"""Editing a sandbox's network rules (FR-12, D15): edit → review → confirm → applied → rollback."""

import html
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvigie.adapters.base import HostContext
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST
from openvigie.collector import commands
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.model import AgentInstance, OpenShellSandbox, SandboxState

FIXTURE = (
    Path(__file__).parents[1] / "openshell" / "fixtures" / "v0.1.4" / "net-policy-base-v2.json"
)
RECORDED = json.loads(FIXTURE.read_text())
TS_NAME = "hub.tail1234.ts.net"
SETTINGS = HubSettings(allowed_hosts=("127.0.0.1", TS_NAME), admins=("alice@example.com",),
                       trust_tailscale_identity=True)  # fmt: skip
ALICE = {"Tailscale-User-Login": "alice@example.com"}
BOB = {"Tailscale-User-Login": "bob@example.com"}
AGENT = AgentInstance("a1", TEST_HOST.host_id, "claude-code", "Claude Code",
                      sandboxed=SandboxState.YES, sandbox_kind="openshell:ov-net")  # fmt: skip
CSRF = re.compile(r'name="csrf" value="([0-9a-f]+)"')
TEXTAREA = re.compile(r"<textarea[^>]*>(.*?)</textarea>", re.S)


def sandbox(network: Mapping[str, Any], version: int, digest: str) -> OpenShellSandbox:
    return OpenShellSandbox(
        "s-net", TEST_HOST.host_id, "ov-net", datetime.now(UTC), agent_ids=("a1",),
        policy_version=version, policy_hash=digest,
        network_policy=json.dumps(network, sort_keys=True, separators=(",", ":")),
    )  # fmt: skip


@pytest.fixture
def app() -> FastAPI:
    db = Database(MEMORY)
    now = datetime.now(UTC)
    Inventory(db).upsert_host(TEST_HOST, now)
    Inventory(db).upsert_agents([AGENT], now)
    box = sandbox(RECORDED["policy"]["network_policies"], 3, RECORDED["hash"])
    SandboxStore(db).replace(TEST_HOST.host_id, [box], now)
    return create_app(SETTINGS, db)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"https://{TS_NAME}", client=("127.0.0.1", 50000),
                      follow_redirects=False)  # fmt: skip


def editor_text(client: TestClient, query: str = "") -> str:
    page = client.get(f"/sandboxes/s-net/rules{query}", headers=ALICE).text
    match = TEXTAREA.search(page)
    assert match, page
    return html.unescape(match.group(1))


def propose(client: TestClient, text: str) -> Any:
    token = CSRF.findall(client.get("/sandboxes/s-net/rules", headers=ALICE).text)[0]
    data = {"csrf": token, "kind": "sandbox.policy", "host_id": TEST_HOST.host_id,
            "workspace": "default", "sandbox": "ov-net", "text": text}  # fmt: skip
    return client.post("/actions/new", data=data, headers=ALICE)


def confirm(client: TestClient, location: str, typed: str = "ov-net") -> Any:
    page = client.get(location, headers=ALICE).text
    token = CSRF.findall(page)[0]  # the confirm form comes first
    return client.post(f"{location}/confirm", data={"csrf": token, "typed": typed}, headers=ALICE)


ADD_DOCS = """  docs:
    endpoints:
      - host: "docs.example.com"
        port: 443
    binaries:
      - path: "/usr/bin/curl"
"""


def test_editor_shows_the_current_rules(client: TestClient) -> None:
    text = editor_text(client)
    assert text.startswith("network_policies:\n  github_readonly:")
    assert '- host: "api.github.com"' in text
    viewer = client.get("/sandboxes/s-net/rules", headers=BOB).text
    assert "<textarea" not in viewer and "github_readonly" in viewer
    assert 'href="/sandboxes/s-net/rules"' in client.get(f"/hosts/{TEST_HOST.host_id}").text


def test_editor_can_start_from_the_starter_policy(client: TestClient) -> None:
    text = editor_text(client, "?from=starter")
    assert '"api.anthropic.com"' in text and "github_readonly" not in text


def test_invalid_text_goes_back_to_the_editor(app: FastAPI, client: TestClient) -> None:
    bad = "network_policies:\n  r: {endpoints: [{host: a.b, port: 99999}]}\n"
    response = propose(client, bad)
    assert response.status_code == 400
    assert "network_policies.r.endpoints.0.port" in response.text
    assert html.unescape(TEXTAREA.search(response.text).group(1)) == bad  # type: ignore[union-attr]
    assert propose(client, editor_text(client)).status_code == 400  # no change
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_review_shows_rules_and_requires_the_sandbox_name(app: FastAPI, client: TestClient) -> None:
    response = propose(client, editor_text(client) + ADD_DOCS)
    assert response.status_code == 303
    location = response.headers["location"]
    page = client.get(location, headers=ALICE).text
    assert "added rule docs" in page and "docs.example.com:443 (audit only)" in page
    assert "Type <code>ov-net</code> to confirm" in page
    assert confirm(client, location, "ov").status_code == 400
    assert confirm(client, location).status_code == 303
    (command,) = [CommandQueue(app.state.db).get(r[0]) for r in app.state.db.conn.execute(
        "SELECT command_id FROM commands")]  # fmt: skip
    assert command is not None and command.action == "sandbox.policy.set"
    assert command.args["base_hash"] == RECORDED["hash"] and command.args["sandbox"] == "ov-net"
    assert "docs" in json.loads(command.args["network_policies"])


class FakeOpenShell:
    """`policy get` and `policy set` for one sandbox."""

    def __init__(self) -> None:
        self.version, self.body = 3, RECORDED["policy"]

    def digest(self) -> str:
        return f"{self.version:064x}" if self.version != 3 else RECORDED["hash"]

    def __call__(self, args: Sequence[str]) -> str:
        if list(args[:2]) == ["policy", "get"]:
            return json.dumps({"version": self.version, "active_version": self.version,
                               "hash": self.digest(), "policy": self.body})  # fmt: skip
        self.body = json.loads(Path(args[args.index("--policy") + 1]).read_text())
        self.version += 1
        return "loaded"


def test_apply_and_roll_back_end_to_end(
    app: FastAPI, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOpenShell()
    monkeypatch.setattr(commands, "cli_runner", lambda timeout: fake)
    token = Credentials(app.state.db).issue_token(TEST_HOST.host_id, datetime.now(UTC))

    def transport(method: str, url: str, body: Any, headers: Any, timeout: float) -> Any:
        response = client.request(method, url.split("7878", 1)[1], content=body, headers=headers)
        return response.status_code, response.content

    ctx = HostContext(host=TEST_HOST, homes=(tmp_path,), process_source=list,
                      sandbox_probe=lambda pid: (SandboxState.NO, None))  # fmt: skip
    settings = CollectorSettings(homes=(tmp_path,), allow_actions=("sandbox.policy.set",))
    hub = HubClient("http://127.0.0.1:7878", token=token, transport=transport)
    collector = Collector(settings, ctx, Discovery(adapters=[]), hub)

    location = propose(client, editor_text(client) + ADD_DOCS).headers["location"]
    assert confirm(client, location).status_code == 303
    assert collector.tick() and collector.tick()
    assert fake.version == 4 and "docs" in fake.body["network_policies"]
    page = client.get(location, headers=ALICE).text
    assert "succeeded" in page and "policy revision 4 loaded (was 3)" in page

    # the collector reports the new state; then roll back to the rules before the change
    SandboxStore(app.state.db).replace(TEST_HOST.host_id, [sandbox(
        fake.body["network_policies"], 4, fake.digest())], datetime.now(UTC))  # fmt: skip
    undo_token = CSRF.findall(client.get(location, headers=ALICE).text)[-1]
    undo = client.post(f"{location}/rollback", data={"csrf": undo_token}, headers=ALICE)
    assert undo.status_code == 303
    undo_page = client.get(undo.headers["location"], headers=ALICE).text
    assert "removed rule docs" in undo_page and "Undoes" in undo_page
    assert confirm(client, undo.headers["location"]).status_code == 303
    assert collector.tick() and collector.tick()
    assert fake.version == 5 and fake.body["network_policies"] == RECORDED["policy"][
        "network_policies"]  # fmt: skip
    results = [e.result for e in reversed(AuditLog(app.state.db).entries())]
    assert results == ["proposed", "confirmed", "delivered", "succeeded",
                       "proposed", "confirmed", "delivered", "succeeded"]  # fmt: skip
    assert AuditLog(app.state.db).verify().ok
