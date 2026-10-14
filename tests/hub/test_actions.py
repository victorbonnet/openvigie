"""Write actions (FR-17, D14): propose → diff → typed confirm → command → result, audited."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openvigie.adapters.base import HostContext
from openvigie.adapters.registry import Discovery
from openvigie.adapters.testing import TEST_HOST
from openvigie.collector.client import HubClient
from openvigie.collector.daemon import Collector
from openvigie.config import CollectorSettings, HubSettings
from openvigie.hub.actions import KINDS, Kind, Plan, StoredAction
from openvigie.hub.app import create_app
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database, ts
from openvigie.hub.store.inventory import Inventory
from openvigie.model import SandboxState

TS_NAME = "hub.tail1234.ts.net"
SETTINGS = HubSettings(allowed_hosts=("127.0.0.1", TS_NAME), admins=("alice@example.com",),
                       trust_tailscale_identity=True)  # fmt: skip
ALICE = {"Tailscale-User-Login": "alice@example.com"}
BOB = {"Tailscale-User-Login": "bob@example.com"}
FORM = {"content-type": "application/x-www-form-urlencoded"}
CSRF = re.compile(r'action="([^"]+)"[^<]*<input type="hidden" name="csrf" value="([0-9a-f]+)"')


def flip_plan(db: Database, params: Mapping[str, str]) -> Plan:
    value = params.get("host_id", "")
    return Plan(TEST_HOST.host_id, TEST_HOST.hostname, "noop", {"value": value},
                f"Set to {value}", before="old\n", after=f"{value}\n")  # fmt: skip


def flip_undo(action: StoredAction) -> dict[str, str]:
    return {"host_id": action.plan.before.strip()}


@pytest.fixture
def app() -> FastAPI:
    db = Database(MEMORY)
    Inventory(db).upsert_host(TEST_HOST, datetime.now(UTC))
    app = create_app(SETTINGS, db)
    app.state.action_kinds = {**KINDS, "flip": Kind(flip_plan, flip_undo)}
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"https://{TS_NAME}", client=("127.0.0.1", 50000),
                      follow_redirects=False)  # fmt: skip


def forms(client: TestClient, page: str, who: Mapping[str, str] = ALICE) -> dict[str, str]:
    """Form action → CSRF token, as rendered for ``who``."""
    return dict(CSRF.findall(client.get(page, headers=who).text))


def post(
    client: TestClient,
    url: str,
    data: dict[str, str],
    who: Mapping[str, str] = ALICE,
    extra: Mapping[str, str] | None = None,
) -> Any:
    return client.post(url, data=data, headers={**who, **(extra or {})})


def propose_noop(client: TestClient) -> str:
    token = forms(client, f"/hosts/{TEST_HOST.host_id}")["/actions/new"]
    response = post(client, "/actions/new",
                    {"csrf": token, "kind": "noop", "host_id": TEST_HOST.host_id})  # fmt: skip
    assert response.status_code == 303, response.text
    return str(response.headers["location"]).rsplit("/", 1)[1]


def confirm(client: TestClient, action_id: str, typed: str) -> Any:
    token = forms(client, f"/actions/{action_id}")[f"/actions/{action_id}/confirm"]
    return post(client, f"/actions/{action_id}/confirm", {"csrf": token, "typed": typed})


def collector_for(app: FastAPI, client: TestClient, tmp_path: Path) -> Collector:
    token = Credentials(app.state.db).issue_token(TEST_HOST.host_id, datetime.now(UTC))

    def transport(method: str, url: str, body: Any, headers: Any, timeout: float) -> Any:
        response = client.request(method, url.split("7878", 1)[1], content=body, headers=headers)
        return response.status_code, response.content

    ctx = HostContext(host=TEST_HOST, homes=(tmp_path,), process_source=list,
                      sandbox_probe=lambda pid: (SandboxState.NO, None))  # fmt: skip
    hub = HubClient("http://127.0.0.1:7878", token=token, transport=transport)
    settings = CollectorSettings(homes=(tmp_path,), allow_actions=("noop",))
    return Collector(settings, ctx, Discovery(adapters=[]), hub)


def test_noop_end_to_end(app: FastAPI, client: TestClient, tmp_path: Path) -> None:
    action_id = propose_noop(client)
    page = client.get(f"/actions/{action_id}", headers=ALICE).text
    assert "Send a test command (noop) to testhost" in page
    assert '<span class="add">+testhost runs noop and reports back</span>' in page
    assert "Type <code>testhost</code> to confirm" in page

    assert confirm(client, action_id, "testhost").status_code == 303
    collector = collector_for(app, client, tmp_path)
    assert collector.tick() and collector.tick()  # delivered, then the result is reported
    page = client.get(f"/actions/{action_id}", headers=ALICE).text
    assert "confirmed" in page and "succeeded" in page and "did nothing, as asked" in page

    log = AuditLog(app.state.db)
    assert [(e.action, e.result, e.actor) for e in reversed(log.entries())] == [
        ("action.noop", "proposed", "alice@example.com"),
        ("action.noop", "confirmed", "alice@example.com"),
        ("command.noop", "delivered", "host:testhost"),
        ("command.noop", "succeeded", "host:testhost"),
    ]
    assert log.verify().ok
    assert "Send a test command (noop) to testhost" in client.get("/actions").text


def test_viewers_see_no_forms_and_cannot_post(app: FastAPI, client: TestClient) -> None:
    assert forms(client, f"/hosts/{TEST_HOST.host_id}", BOB) == {}
    action_id = propose_noop(client)
    assert forms(client, f"/actions/{action_id}", BOB) == {}
    alice_token = forms(client, f"/actions/{action_id}")[f"/actions/{action_id}/confirm"]
    response = post(client, f"/actions/{action_id}/confirm",
                    {"csrf": alice_token, "typed": "testhost"}, BOB)  # fmt: skip
    assert response.status_code == 403
    assert post(client, "/actions/new", {"csrf": "x", "kind": "noop"}, {}).status_code == 403
    refusals = [e for e in AuditLog(app.state.db).entries() if e.result == "refused"]
    assert {e.actor for e in refusals} == {"bob@example.com", "anonymous"}


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
    ],
)
def test_cross_site_posts_are_refused(client: TestClient, headers: dict[str, str]) -> None:
    action_id = propose_noop(client)
    token = forms(client, f"/actions/{action_id}")[f"/actions/{action_id}/confirm"]
    data = {"csrf": token, "typed": "testhost"}
    assert post(client, f"/actions/{action_id}/confirm", data, extra=headers).status_code == 403
    same = {"origin": f"https://{TS_NAME}", "sec-fetch-site": "same-origin"}
    assert post(client, f"/actions/{action_id}/confirm", data, extra=same).status_code == 303


def test_bad_csrf_tokens_are_refused(client: TestClient) -> None:
    action_id = propose_noop(client)
    url = f"/actions/{action_id}/confirm"
    assert post(client, url, {"typed": "testhost"}).status_code == 422  # missing: invalid form
    assert post(client, url, {"csrf": "0" * 64, "typed": "testhost"}).status_code == 403
    other = forms(client, f"/hosts/{TEST_HOST.host_id}")["/actions/new"]  # another purpose
    assert post(client, url, {"csrf": other, "typed": "testhost"}).status_code == 403


def test_wrong_typed_name_is_refused_and_audited(app: FastAPI, client: TestClient) -> None:
    action_id = propose_noop(client)
    response = confirm(client, action_id, "testhos")
    assert response.status_code == 400 and "type testhost exactly to confirm" in response.text
    assert CommandQueue(app.state.db).get("") is None
    assert AuditLog(app.state.db).entries()[0].result == "refused"
    assert confirm(client, action_id, " testhost ").status_code == 303


def test_a_proposal_is_confirmed_once(app: FastAPI, client: TestClient) -> None:
    action_id = propose_noop(client)
    token = forms(client, f"/actions/{action_id}")[f"/actions/{action_id}/confirm"]
    url = f"/actions/{action_id}/confirm"
    assert post(client, url, {"csrf": token, "typed": "testhost"}).status_code == 303
    again = post(client, url, {"csrf": token, "typed": "testhost"})
    assert again.status_code == 400 and "this proposal is confirmed" in again.text
    with app.state.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_expired_and_cancelled_proposals_cannot_be_confirmed(
    app: FastAPI, client: TestClient
) -> None:
    expired = propose_noop(client)
    with app.state.db.transaction() as conn:
        conn.execute("UPDATE actions SET expires_at = ? WHERE action_id = ?",
                     (ts(datetime.now(UTC) - timedelta(seconds=1)), expired))  # fmt: skip
    page = client.get(f"/actions/{expired}", headers=ALICE).text
    assert "expired" in page and "to confirm" not in page

    cancelled = propose_noop(client)
    decide = forms(client, f"/actions/{cancelled}")
    cancel_token = decide[f"/actions/{cancelled}/cancel"]
    assert post(client, f"/actions/{cancelled}/cancel", {"csrf": cancel_token}).status_code == 303
    data = {"csrf": decide[f"/actions/{cancelled}/confirm"], "typed": "testhost"}
    response = post(client, f"/actions/{cancelled}/confirm", data)
    assert response.status_code == 400 and "this proposal is cancelled" in response.text


def test_rollback_proposes_the_inverse(app: FastAPI, client: TestClient) -> None:
    token = forms(client, f"/hosts/{TEST_HOST.host_id}")["/actions/new"]
    created = post(client, "/actions/new", {"csrf": token, "kind": "flip", "host_id": "new"})
    action_id = created.headers["location"].rsplit("/", 1)[1]
    assert confirm(client, action_id, "testhost").status_code == 303
    undo_token = forms(client, f"/actions/{action_id}")[f"/actions/{action_id}/rollback"]
    undo = post(client, f"/actions/{action_id}/rollback", {"csrf": undo_token})
    assert undo.status_code == 303
    undo_page = client.get(undo.headers["location"], headers=ALICE).text
    assert "Set to old" in undo_page and "Undoes" in undo_page


def test_noop_has_nothing_to_roll_back(client: TestClient) -> None:
    action_id = propose_noop(client)
    assert confirm(client, action_id, "testhost").status_code == 303
    assert f"/actions/{action_id}/rollback" not in forms(client, f"/actions/{action_id}")


def test_only_small_url_encoded_forms(client: TestClient) -> None:
    token = forms(client, f"/hosts/{TEST_HOST.host_id}")["/actions/new"]
    json_body = client.post("/actions/new", json={"csrf": token, "kind": "noop"}, headers=ALICE)
    assert json_body.status_code == 415
    big = post(client, "/actions/new", {"csrf": token, "kind": "noop", "host_id": "x" * 20000})
    assert big.status_code == 413
    extra = post(client, "/actions/new", {"csrf": token, "kind": "noop", "evil": "1"})
    assert extra.status_code == 422
    unknown = post(client, "/actions/new", {"csrf": token, "kind": "rm-rf"})
    assert unknown.status_code == 400
