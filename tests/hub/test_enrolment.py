from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie import cli
from openvigie.collector.client import HubClient, HubError, check_hub_url
from openvigie.collector.enrol import enrol, save_hub_url
from openvigie.config import CollectorSettings, HubSettings, read_private_file
from openvigie.hub.app import create_app
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.model import Host, to_dict

PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
MAC = Host("h-mac", "mbp", "darwin", "arm64", "0.0.1")


@pytest.fixture
def db() -> Database:
    return Database(MEMORY)


@pytest.fixture
def client(db: Database) -> TestClient:
    return TestClient(create_app(HubSettings(), db), base_url="http://127.0.0.1:7878")


def new_code(db: Database, ttl: timedelta = timedelta(minutes=15), **kw: str) -> str:
    return Credentials(db).create_enrol_code(datetime.now(UTC), ttl, **kw)


def post_enrol(client: TestClient, code: str, host: Host = PI) -> object:
    return client.post("/api/v1/enrol", json={"code": code, "host": to_dict(host)})


def test_enrol_issues_token_once(client: TestClient, db: Database) -> None:
    code = new_code(db)
    response = post_enrol(client, code)
    assert response.status_code == 201  # type: ignore[attr-defined]
    token = response.json()["token"]  # type: ignore[attr-defined]
    assert token.startswith("ovh_")
    assert Credentials(db).verify_token(token, datetime.now(UTC)) == "h-pi"
    with db.read() as conn:
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM host_tokens")]
    assert token not in stored  # only the hash is stored
    assert post_enrol(client, code).status_code == 403  # type: ignore[attr-defined]


def test_expired_code_rejected(client: TestClient, db: Database) -> None:
    code = new_code(db, ttl=timedelta(seconds=-1))
    assert post_enrol(client, code).status_code == 403  # type: ignore[attr-defined]


def test_unknown_code_and_bad_body(client: TestClient) -> None:
    assert post_enrol(client, "ove_nope").status_code == 403  # type: ignore[attr-defined]
    assert client.post("/api/v1/enrol", json={"code": "x"}).status_code == 422
    assert client.post("/api/v1/enrol", content=b"[1]").status_code == 422


def test_unbound_code_cannot_take_over_enrolled_host(client: TestClient, db: Database) -> None:
    post_enrol(client, new_code(db))
    response = post_enrol(client, new_code(db))
    assert response.status_code == 409  # type: ignore[attr-defined]
    bound = new_code(db, bound_host_id="h-pi")
    assert post_enrol(client, bound).status_code == 201  # type: ignore[attr-defined]


def test_new_token_revokes_previous(db: Database) -> None:
    creds = Credentials(db)
    from openvigie.hub.store.inventory import Inventory

    Inventory(db).upsert_host(PI, datetime.now(UTC))
    first = creds.issue_token("h-pi", datetime.now(UTC))
    second = creds.issue_token("h-pi", datetime.now(UTC))
    assert creds.verify_token(first, datetime.now(UTC)) is None
    assert creds.verify_token(second, datetime.now(UTC)) == "h-pi"
    assert creds.verify_token("garbage", datetime.now(UTC)) is None


def test_failed_attempts_are_rate_limited(client: TestClient) -> None:
    for _ in range(10):
        assert post_enrol(client, "ove_bad").status_code == 403  # type: ignore[attr-defined]
    assert post_enrol(client, "ove_bad").status_code == 429  # type: ignore[attr-defined]


def test_check_hub_url() -> None:
    assert check_hub_url("http://127.0.0.1:7878/") == "http://127.0.0.1:7878"
    assert check_hub_url("https://hub.tail1234.ts.net") == "https://hub.tail1234.ts.net"
    for bad in ["http://192.168.1.5:7878", "ftp://x", "https://u:p@hub", "hub:7878"]:
        with pytest.raises(ValueError):
            check_hub_url(bad)


def transport_for(client: TestClient):  # type: ignore[no-untyped-def]
    def transport(method, url, body, headers, timeout):  # type: ignore[no-untyped-def]
        path = url.split("7878", 1)[1]
        response = client.request(method, path, content=body, headers=headers)
        return response.status_code, response.content

    return transport


def test_collector_enrol_end_to_end(client: TestClient, db: Database, tmp_path: Path) -> None:
    config = tmp_path / "cfg" / "collector.toml"
    config.parent.mkdir()
    config.write_text('homes = ["/home/pi"]\nhub_url = "http://old"\n')
    settings = CollectorSettings(token_file=tmp_path / "state" / "token")
    token_file = enrol(
        "http://127.0.0.1:7878", new_code(db), MAC, settings, config_path=config,
        transport=transport_for(client),
    )  # fmt: skip
    token = read_private_file(token_file)
    assert Credentials(db).verify_token(token, datetime.now(UTC)) == "h-mac"
    assert config.read_text() == 'homes = ["/home/pi"]\nhub_url = "http://127.0.0.1:7878"\n'


def test_collector_enrol_reports_hub_errors(client: TestClient, tmp_path: Path) -> None:
    settings = CollectorSettings(token_file=tmp_path / "token")
    with pytest.raises(HubError, match="403"):
        enrol("http://127.0.0.1:7878", "ove_bad", MAC, settings,
              config_path=tmp_path / "c.toml", transport=transport_for(client))  # fmt: skip
    assert not (tmp_path / "token").exists()


def test_save_hub_url_creates_file(tmp_path: Path) -> None:
    path = tmp_path / "new" / "collector.toml"
    save_hub_url(path, "https://hub.ts.net")
    assert path.read_text() == 'hub_url = "https://hub.ts.net"\n'


def test_hub_client_sends_bearer(client: TestClient) -> None:
    seen = {}

    def transport(method, url, body, headers, timeout):  # type: ignore[no-untyped-def]
        seen.update(headers)
        return 200, b"{}"

    HubClient("http://127.0.0.1:7878", token="ovh_x", transport=transport).post("/p", {})  # noqa: S106
    assert seen["authorization"] == "Bearer ovh_x"


def test_cli_enrol_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("OPENVIGIE_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["hub", "enrol-code", "--ttl", "5m"]) == 0
    out = capsys.readouterr().out
    assert "valid 5 min" in out and "openvigie enrol http://127.0.0.1:7878 ove_" in out
    with pytest.raises(SystemExit):
        cli.main(["hub", "enrol-code", "--ttl", "soon"])
