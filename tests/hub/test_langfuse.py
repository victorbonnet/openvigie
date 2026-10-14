import base64
import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openvigie import semconv
from openvigie.config import (
    ConfigError,
    HubSettings,
    LangfuseSettings,
    Retention,
    load_hub_settings,
)
from openvigie.hub import langfuse
from openvigie.hub.app import create_app
from openvigie.hub.langfuse import LangfuseExporter
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import MEMORY, Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.store.spans import SpanQueue
from openvigie.model import (
    SCHEMA_VERSION,
    AgentInstance,
    Host,
    Span,
    SpanStatus,
    to_dict,
)

SECRET = "sk-lf-0123456789abcdef-very-secret"  # noqa: S105 - test value
PUBLIC = "pk-lf-public"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
PI = Host("h-pi", "pi5", "linux", "aarch64", "0.0.1")
FAKE_KEY = "sk-ant-api03-" + "z" * 90


class Stub:
    """A local HTTP server standing in for Langfuse; replies with the queued status codes."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                stub.requests.append(
                    {"path": self.path, "headers": dict(self.headers), "body": json.loads(body)}
                )
                self.send_response(stub.statuses.pop(0) if stub.statuses else 200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[Stub]:
    server = Stub()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", SECRET)


def settings_for(url: str, **extra: Any) -> LangfuseSettings:
    return LangfuseSettings(host=url, public_key=PUBLIC, **extra)


def seeded() -> Database:
    db = Database(MEMORY)
    inv = Inventory(db)
    inv.upsert_host(PI, NOW)
    inv.upsert_agents([AgentInstance("a1", "h-pi", "claude-code", "Claude Code")], NOW)
    return db


def spans(count: int = 3, **attrs: Any) -> list[Span]:
    root = Span("t" * 32, "r" * 16, "invoke_agent claude-code", NOW, NOW + timedelta(seconds=9),
                agent_id="a1", attributes={semconv.SESSION_ID: "s1"})  # fmt: skip
    children = [
        Span("t" * 32, f"{i:016x}", "chat claude-opus-5-5", NOW, NOW + timedelta(seconds=1),
             parent_span_id=root.span_id, agent_id="a1", status=SpanStatus.OK,
             attributes={semconv.GEN_AI_OPERATION_NAME: semconv.OP_CHAT,
                         semconv.GEN_AI_USAGE_OUTPUT_TOKENS: 42, **attrs})
        for i in range(count - 1)
    ]  # fmt: skip
    return [root, *children]


def exporter(db: Database, url: str, **extra: Any) -> LangfuseExporter:
    return LangfuseExporter(settings_for(url, **extra), db)


def test_otlp_json_shape_and_auth(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    assert exporter(db, stub.url).run_once(NOW) == 3

    (request,) = stub.requests
    assert request["path"] == "/api/public/otel/v1/traces"
    assert request["headers"]["Content-Type"] == "application/json"
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert request["headers"]["Authorization"] == f"Basic {expected}"
    (resource,) = request["body"]["resourceSpans"]
    assert {"key": "host.id", "value": {"stringValue": "h-pi"}} in resource["resource"][
        "attributes"
    ]
    otlp = resource["scopeSpans"][0]["spans"]
    root = next(s for s in otlp if "parentSpanId" not in s)
    chat = next(s for s in otlp if s["kind"] == langfuse.KIND_CLIENT)
    assert root["traceId"] == "t" * 32 and root["spanId"] == "r" * 16
    assert root["startTimeUnixNano"] == str(int(NOW.timestamp()) * 10**9)
    assert {"key": "langfuse.session.id", "value": {"stringValue": "s1"}} in root["attributes"]
    assert chat["parentSpanId"] == "r" * 16 and chat["status"] == {"code": 1}
    tokens = {"key": semconv.GEN_AI_USAGE_OUTPUT_TOKENS, "value": {"intValue": "42"}}
    assert tokens in chat["attributes"]
    assert SpanQueue(db).stats().exported == 3 and SpanQueue(db).stats().pending == 0
    assert exporter(db, stub.url).run_once(NOW) == 0  # nothing re-sent


def test_retries_5xx_with_backoff(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    stub.statuses = [503]
    lf = exporter(db, stub.url)
    assert lf.run_once(NOW) == 0
    assert lf.last_error == "Langfuse unavailable (HTTP 503)"
    assert lf.run_once(NOW + timedelta(seconds=1)) == 0  # still backing off: no request
    assert len(stub.requests) == 1
    assert lf.run_once(NOW + langfuse.FIRST_BACKOFF) == 3
    assert lf.last_error is None and len(stub.requests) == 2


def test_gives_up_after_max_attempts(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    stub.statuses = [500] * langfuse.MAX_ATTEMPTS
    lf = exporter(db, stub.url)
    when = NOW
    for _ in range(langfuse.MAX_ATTEMPTS):
        lf.run_once(when)
        when += langfuse.MAX_BACKOFF
    assert len(stub.requests) == langfuse.MAX_ATTEMPTS
    assert SpanQueue(db).stats() == SpanQueue(db).stats().__class__(0, 0, 3)


def test_no_retry_storm_on_4xx(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    stub.statuses = [400]
    lf = exporter(db, stub.url)
    lf.run_once(NOW)
    lf.run_once(NOW + timedelta(hours=1))
    assert len(stub.requests) == 1  # the bad batch is dropped, not resent
    assert SpanQueue(db).stats().dropped == 3


def test_auth_failure_pauses_without_dropping(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    stub.statuses = [401]
    lf = exporter(db, stub.url)
    lf.run_once(NOW)
    lf.run_once(NOW + timedelta(minutes=1))
    assert len(stub.requests) == 1
    assert SpanQueue(db).stats().pending == 3 and SpanQueue(db).stats().dropped == 0
    assert lf.run_once(NOW + langfuse.AUTH_PAUSE) == 3


def test_redaction_and_content_gate(stub: Stub) -> None:
    content = {semconv.GEN_AI_OUTPUT_MESSAGES: "hello", "note": f"key={FAKE_KEY}"}
    for export_content in (False, True):
        db = seeded()
        SpanQueue(db).enqueue(spans(2, **content), "h-pi", NOW)
        exporter(db, stub.url, export_content=export_content).run_once(NOW)
        body = json.dumps(stub.requests[-1]["body"])
        assert FAKE_KEY not in body and "[REDACTED" in body  # redacted again on the hub
        assert (semconv.GEN_AI_OUTPUT_MESSAGES in body) is export_content


def test_secret_never_logged(stub: Stub, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    lf = exporter(db, stub.url)
    stub.statuses = [401]
    lf.run_once(NOW)
    stub.close()  # connection refused from now on
    unreachable = exporter(db, stub.url)
    unreachable.run_once(NOW + timedelta(hours=1))
    assert unreachable.last_error and "cannot reach Langfuse" in unreachable.last_error
    for text in (caplog.text, repr(lf), lf.last_error or "", unreachable.last_error):
        assert SECRET not in text and base64.b64encode(SECRET.encode()).decode() not in text


def test_redirects_are_not_followed(stub: Stub) -> None:
    db = seeded()
    SpanQueue(db).enqueue(spans(), "h-pi", NOW)
    stub.statuses = [307]
    lf = exporter(db, stub.url)
    lf.run_once(NOW)
    assert len(stub.requests) == 1 and SpanQueue(db).stats().dropped == 3


def test_queue_is_pruned_and_hosts_cannot_write_others_spans() -> None:
    db = seeded()
    queue = SpanQueue(db)
    assert queue.enqueue(spans(), "h-other", NOW) == 0  # not this host's agent
    assert queue.enqueue(spans(), "h-pi", NOW) == 3
    assert queue.prune(NOW - timedelta(hours=1)) == 0
    assert queue.prune(NOW + timedelta(seconds=1)) == 3
    assert queue.stats().dropped == 3


def token_for(db: Database) -> str:
    creds = Credentials(db)
    return creds.issue_token(PI.host_id, NOW)


def push(client: TestClient, token: str, body: dict[str, Any]) -> Any:
    return client.post("/api/v1/ingest", json=body, headers={"Authorization": f"Bearer {token}"})


def ingest_body() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "host": to_dict(PI),
        "sent_at": datetime.now(UTC).isoformat(),
        "agents": [to_dict(AgentInstance("a1", "h-pi", "claude-code", "Claude Code"))],
        "spans": [to_dict(s) for s in spans()],
    }


def test_ingest_queues_spans_only_when_configured(stub: Stub) -> None:
    for configured in (False, True):
        db = seeded()
        lf = settings_for(stub.url) if configured else None
        app = create_app(HubSettings(langfuse=lf), db)
        client = TestClient(app, base_url="http://127.0.0.1:7878")
        response = push(client, token_for(db), ingest_body()).json()
        assert response["accepted"]["spans"] == (3 if configured else 0)
        assert response["traces"] == {"wanted": configured, "export_content": False}
        assert SpanQueue(db).stats().pending == (3 if configured else 0)
        page = client.get("/settings").text
        assert ("3 waiting" in page) is configured
        assert SECRET not in page
    assert stub.requests == []  # the lifespan never ran: nothing sent without a running hub


def test_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "hub.toml"
    path.write_text('[langfuse]\nhost = "https://cloud.langfuse.com"\npublic_key = "pk-lf-1"\n')
    settings = load_hub_settings(path)
    assert settings.langfuse == LangfuseSettings("https://cloud.langfuse.com", "pk-lf-1")
    assert settings.retention == Retention(span_queue_hours=24)
    for bad in ('host = "http://langfuse.example.com"\npublic_key = "pk-lf-1"',
                'host = "https://u:p@cloud.langfuse.com"\npublic_key = "pk-lf-1"',
                f'host = "https://cloud.langfuse.com"\npublic_key = "{SECRET}"'):  # fmt: skip
        path.write_text(f"[langfuse]\n{bad}\n")
        with pytest.raises(ConfigError):
            load_hub_settings(path)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY")
    with pytest.raises(ConfigError, match="secret_key_file"):
        settings.langfuse.secret_key()
    key_file = tmp_path / "secret"
    key_file.write_text(SECRET + "\n")
    key_file.chmod(0o600)
    from_file = LangfuseSettings("https://cloud.langfuse.com", "pk-lf-1", key_file)
    assert from_file.secret_key() == SECRET
    key_file.chmod(0o644)
    with pytest.raises(ConfigError, match="permissions"):
        from_file.secret_key()
