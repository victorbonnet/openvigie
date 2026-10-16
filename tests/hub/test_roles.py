"""Viewer and admin roles from Tailscale identity (FR-17, D14)."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.roles import ANONYMOUS, Admin, RefusalBudget, identity, resolve
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.db import MEMORY, Database

TS_NAME = "hub.tail1234.ts.net"
TRUSTED = HubSettings(allowed_hosts=("127.0.0.1", TS_NAME), admins=("alice@example.com",),
                      trust_tailscale_identity=True)  # fmt: skip
ALICE = {"Tailscale-User-Login": "alice@example.com", "Tailscale-User-Name": "Alice"}
BOB = {"Tailscale-User-Login": "bob@example.com"}


def make_app(settings: HubSettings) -> FastAPI:
    app = create_app(settings, Database(MEMORY))

    @app.get("/_whoami")
    def whoami(request: Request) -> dict[str, object]:
        who = identity(request)
        return {"login": who.login, "role": who.role, "headers": list(request.headers)}

    @app.post("/api/_write")
    def write(who: Admin) -> dict[str, str]:
        return {"by": who.actor}

    return app


def client_for(settings: HubSettings, peer: str = "127.0.0.1") -> TestClient:
    return TestClient(make_app(settings), base_url=f"https://{TS_NAME}", client=(peer, 50000))


def test_listed_login_is_admin_and_others_viewers() -> None:
    client = client_for(TRUSTED)
    alice = client.get("/_whoami", headers=ALICE).json()
    assert (alice["login"], alice["role"]) == ("alice@example.com", "admin")
    assert not any(h.startswith("tailscale-") for h in alice["headers"])
    bob = client.get("/_whoami", headers=BOB).json()
    assert (bob["login"], bob["role"]) == ("bob@example.com", "viewer")
    assert client.get("/_whoami").json()["role"] == "viewer"


def test_forged_headers_are_ignored_without_trust() -> None:
    settings = HubSettings(allowed_hosts=("127.0.0.1", TS_NAME), admins=("alice@example.com",))
    seen = client_for(settings).get("/_whoami", headers=ALICE).json()
    assert (seen["login"], seen["role"]) == (None, "viewer")
    assert not any(h.startswith("tailscale-") for h in seen["headers"])


def test_identity_needs_a_loopback_peer_and_the_tailscale_name() -> None:
    assert (
        client_for(TRUSTED, peer="192.168.1.20").get("/_whoami", headers=ALICE).json()["role"]
        == "viewer"
    )
    local = TestClient(make_app(TRUSTED), base_url="http://127.0.0.1:7878", client=("127.0.0.1", 1))
    assert local.get("/_whoami", headers=ALICE).json()["login"] is None


@pytest.mark.parametrize(
    ("client", "host", "login", "expected"),
    [
        ("127.0.0.1", TS_NAME, "alice@example.com", "admin"),
        ("::1", f"{TS_NAME}:443", "alice@example.com", "admin"),
        ("127.0.0.1", "localhost:7878", "alice@example.com", None),
        ("127.0.0.1", "[::1]:7878", "alice@example.com", None),
        ("10.0.0.5", TS_NAME, "alice@example.com", None),
        (None, TS_NAME, "alice@example.com", None),
        ("127.0.0.1", TS_NAME, "alice@example.com\nx", None),
        ("127.0.0.1", TS_NAME, "a" * 257, None),
        ("127.0.0.1", TS_NAME, "", None),
    ],
)
def test_resolve(client: str | None, host: str, login: str, expected: str | None) -> None:
    who = resolve(TRUSTED, client, host, login)
    assert (who.role if who.login else None) == expected


def test_require_admin_lets_admins_through_and_audits_refusals() -> None:
    app = make_app(TRUSTED)
    client = TestClient(app, base_url=f"https://{TS_NAME}", client=("127.0.0.1", 50000))
    assert client.post("/api/_write", headers=ALICE).json() == {"by": "alice@example.com"}
    refused = client.post("/api/_write", headers=BOB)
    assert refused.status_code == 403 and refused.json() == {"detail": "admin role required"}
    assert client.post("/api/_write").status_code == 403
    entries = AuditLog(app.state.db).entries()
    assert [(e.actor, e.role, e.action, e.result) for e in entries] == [
        ("anonymous", "viewer", "POST /api/_write", "refused"),
        ("bob@example.com", "viewer", "POST /api/_write", "refused"),
    ]
    assert AuditLog(app.state.db).verify().ok


def test_refusals_are_rate_limited() -> None:
    budget = RefusalBudget(per_minute=2)
    assert [budget.take(60.0), budget.take(61.0), budget.take(62.0)] == [True, True, False]
    assert budget.take(120.0)


def test_header_shows_who_you_are() -> None:
    client = client_for(TRUSTED)
    assert "alice@example.com · admin" in client.get("/", headers=ALICE).text
    assert "local / anonymous · viewer" in client.get("/").text


def test_admins_are_validated() -> None:
    with pytest.raises(ValidationError):
        HubSettings(admins=("not a login",))
    assert HubSettings(admins=("octocat@github",)).admins == ("octocat@github",)
    assert resolve(HubSettings(), "127.0.0.1", TS_NAME, "alice@example.com") is ANONYMOUS


def test_two_login_headers_make_the_request_anonymous() -> None:
    client = client_for(TRUSTED)
    headers = [("Tailscale-User-Login", "bob@example.com"),
               ("Tailscale-User-Login", "alice@example.com")]  # fmt: skip
    seen = client.get("/_whoami", headers=headers).json()
    assert (seen["login"], seen["role"]) == (None, "viewer")


def test_anonymous_cross_site_refusals_are_not_audited() -> None:
    app = make_app(TRUSTED)
    client = TestClient(app, base_url=f"https://{TS_NAME}", client=("127.0.0.1", 50000))
    for _ in range(5):  # what any page the operator visits could send
        assert (
            client.post("/api/_write", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        )
    assert AuditLog(app.state.db).entries() == []
    assert (
        client.post("/api/_write", headers={**BOB, "Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert [e.actor for e in AuditLog(app.state.db).entries()] == ["bob@example.com"]
