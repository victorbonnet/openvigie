import json
import subprocess

from fastapi import Request
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.tailscale import TailscaleInfo, advice, detect

NAME = "hub.tail1234.ts.net"


def runner_with(payload: object):  # type: ignore[no-untyped-def]
    return lambda args: json.dumps(payload)


def test_detect_reads_magicdns_name() -> None:
    info = detect(
        runner_with({"Self": {"DNSName": NAME + "."}}), which=lambda _: "/usr/bin/tailscale"
    )
    assert info == TailscaleInfo(NAME)
    assert info.url == f"https://{NAME}"
    assert info.serve_command(7878) == "tailscale serve --bg --https=443 http://127.0.0.1:7878"


def test_detect_handles_missing_or_broken_tailscale() -> None:
    assert detect(runner_with({}), which=lambda _: None) is None
    assert detect(lambda args: "not json", which=lambda _: "/x") is None
    assert detect(runner_with({"Self": {}}), which=lambda _: "/x") is None

    def failing(args: list[str]) -> str:
        raise subprocess.CalledProcessError(1, args)

    assert detect(failing, which=lambda _: "/x") is None


def test_advice_mentions_allowed_hosts_until_configured() -> None:
    lines = advice(TailscaleInfo(NAME), 7878, ("127.0.0.1", "localhost"))
    assert any("tailscale serve" in line for line in lines)
    assert any(f'"{NAME}"' in line and "allowed_hosts" in line for line in lines)
    configured = advice(TailscaleInfo(NAME), 7878, ("127.0.0.1", NAME))
    assert not any("allowed_hosts" in line for line in configured)
    assert "docs/remote-access.md" in advice(None, 7878, ())[0]


def make_client(hosts: tuple[str, ...]) -> TestClient:
    app = create_app(HubSettings(allowed_hosts=hosts), Database(MEMORY))

    @app.get("/_headers")
    def headers(request: Request) -> dict[str, str]:
        return dict(request.headers)

    return TestClient(app, base_url="http://127.0.0.1:7878")


def test_magicdns_host_accepted_only_when_configured() -> None:
    assert make_client(("127.0.0.1",)).get("/", headers={"host": NAME}).status_code == 400
    assert make_client(("127.0.0.1", NAME)).get("/", headers={"host": NAME}).status_code == 200


def test_identity_headers_are_stripped() -> None:
    client = make_client(("127.0.0.1",))
    seen = client.get(
        "/_headers",
        headers={"Tailscale-User-Login": "alice@example.com", "Tailscale-User-Name": "Alice"},
    ).json()
    assert not any(k.startswith("tailscale-") for k in seen)
