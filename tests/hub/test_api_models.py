"""The API's typed bodies: documented in debug mode, and errors that never echo input."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import MEMORY, Database


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("OPENVIGIE_CONFIG_DIR", str(tmp_path))
    app = create_app(HubSettings(), Database(MEMORY), debug=True)
    return TestClient(app, base_url="http://127.0.0.1:7878")


def test_openapi_documents_the_bodies(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    names = set(schema["components"]["schemas"])
    assert {"IngestRequest", "IngestResponse", "EnrolRequest", "EnrolResponse",
            "AgentsResponse", "UsageResponse", "UsageRecord", "Capabilities"} <= names  # fmt: skip
    usage = schema["components"]["schemas"]["IngestRequest"]["properties"]["usage"]
    assert usage["maxItems"] == 5000


@pytest.mark.parametrize(
    ("method", "url", "body", "status", "detail"),
    [
        ("post", "/api/v1/enrol", b"{not json", 422, "body must be JSON"),
        ("post", "/api/v1/enrol", b'{"code": "<b>sk-ant-x</b>"}', 422, "host: missing"),
        ("post", "/api/v1/enrol", b'{"code": 7, "host": {}}', 422, "code: "),
        ("get", "/api/v1/usage?from=<b>sk-ant-x</b>", None, 422, "query.from: "),
    ],
)
def test_invalid_requests_are_described_without_echo(
    client: TestClient, method: str, url: str, body: bytes | None, status: int, detail: str
) -> None:
    response = client.request(method, url, content=body,
                              headers={"content-type": "application/json"})  # fmt: skip
    assert response.status_code == status
    assert response.json()["detail"].startswith(detail)
    assert "sk-ant-x" not in response.text and "<b>" not in response.text


def test_ui_validation_errors_render_the_error_page(client: TestClient) -> None:
    response = client.get("/usage?days=<b>9999</b>")
    assert response.status_code == 422
    assert "text/html" in response.headers["content-type"] and "<b>9999" not in response.text
