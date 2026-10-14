import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from openvigie import cli
from openvigie.config import HubSettings
from openvigie.hub.app import MAX_BODY_BYTES, create_app, templates_env
from openvigie.hub.store.db import MEMORY, Database


@pytest.fixture
def client() -> TestClient:
    app = create_app(HubSettings(), Database(MEMORY))
    return TestClient(app, base_url="http://127.0.0.1:7878")


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_security_headers_on_every_response(client: TestClient) -> None:
    for path in ["/", "/healthz", "/missing", "/static/app.css"]:
        headers = client.get(path).headers
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"


def test_dns_rebinding_host_rejected(client: TestClient) -> None:
    response = client.get("/", headers={"host": "evil.example.com"})
    assert response.status_code == 400
    assert "content-security-policy" in response.headers


def test_localhost_host_accepted(client: TestClient) -> None:
    assert client.get("/", headers={"host": "localhost:7878"}).status_code == 200


def test_docs_disabled(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_body_limit(client: TestClient) -> None:
    response = client.post("/healthz", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413


def test_index_uses_vendored_htmx_only(client: TestClient) -> None:
    html = client.get("/").text
    assert '<script src="/static/vendor/htmx-2.0.4.min.js" defer></script>' in html
    assert "http" not in html.split("<body>")[0].replace("http-equiv", "")
    assert client.get("/static/vendor/htmx-2.0.4.min.js").status_code == 200


def test_autoescape() -> None:
    env = templates_env()
    out = env.from_string("{{ name }}").render(name="<script>alert(1)</script>")
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_serve_refuses_public_bind(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "serve", "--bind", "0.0.0.0"]) == 2  # noqa: S104
    assert "not a loopback address" in capsys.readouterr().err


def test_collector_never_imports_hub_dependencies() -> None:
    code = (
        "import sys, openvigie.cli, openvigie.collector.scan, openvigie.collector.host;"
        "bad = [m for m in ('fastapi', 'starlette', 'uvicorn', 'jinja2') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0  # noqa: S603
