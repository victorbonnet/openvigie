"""Sending alerts to a webhook (FR-14, D17), against a real local HTTP server."""

import json
import logging
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openvigie.config import AlertSettings, HubSettings, WebhookSettings
from openvigie.hub.notify import MAX_ATTEMPTS, WebhookNotifier
from openvigie.hub.store.alerts import AlertStore
from openvigie.hub.store.db import MEMORY, Database

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
TOKEN = "whsec-" + "t" * 32
SECRET_PATH = "/hooks/T000/B000/" + "s" * 24


class Hook:
    """A local webhook receiver; ``replies`` are the statuses to answer, in order."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.replies: list[int] = []
        hook = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                hook.received.append({"path": self.path, "auth": self.headers["Authorization"],
                                      "body": json.loads(body)})  # fmt: skip
                status = hook.replies.pop(0) if hook.replies else 200
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}{SECRET_PATH}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def hook() -> Iterator[Hook]:
    h = Hook()
    yield h
    h.server.shutdown()


class Clock:
    t = 1000.0

    def __call__(self) -> float:
        return self.t


def notifier(hook: Hook, db: Database, tmp_path: Path, clock: Clock) -> WebhookNotifier:
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    return WebhookNotifier(WebhookSettings(url=hook.url, token_file=token), db, clock=clock)


def raised(db: Database, key: str = "k1") -> None:
    AlertStore(db).raise_once(key, "exceeded", "Budget daily is over its limit today",
                              "all agents: $6.00 of $5.00", NOW)  # fmt: skip


def test_delivered_once(hook: Hook, tmp_path: Path) -> None:
    db = Database(MEMORY)
    raised(db)
    n = notifier(hook, db, tmp_path, Clock())
    assert n.run_once(NOW) == 1 and n.run_once(NOW) == 0
    (got,) = hook.received
    assert got["path"] == SECRET_PATH and got["auth"] == f"Bearer {TOKEN}"
    assert got["body"]["level"] == "exceeded" and got["body"]["source"] == "openvigie"
    assert (
        got["body"]["text"]
        == got["body"]["content"]
        == ("[openvigie] Budget daily is over its limit today: all agents: $6.00 of $5.00")
    )
    (alert,) = AlertStore(db).recent()
    assert alert.delivered_at == NOW and alert.attempts == 1


def test_retried_after_a_failure_with_a_pause(hook: Hook, tmp_path: Path) -> None:
    db = Database(MEMORY)
    raised(db)
    clock = Clock()
    n = notifier(hook, db, tmp_path, clock)
    hook.replies = [500]
    assert n.run_once(NOW) == 0
    assert AlertStore(db).recent()[0].last_error == "HTTP 500"
    assert n.run_once(NOW) == 0 and len(hook.received) == 1  # still pausing
    clock.t += 31
    assert n.run_once(NOW) == 1 and len(hook.received) == 2


def test_gives_up_after_max_attempts(hook: Hook, tmp_path: Path) -> None:
    db = Database(MEMORY)
    raised(db)
    clock = Clock()
    n = notifier(hook, db, tmp_path, clock)
    hook.replies = [503] * 10
    for _ in range(MAX_ATTEMPTS + 2):
        n.run_once(NOW)
        clock.t += 10_000
    assert len(hook.received) == MAX_ATTEMPTS
    (alert,) = AlertStore(db).recent()
    assert alert.delivered_at is None and alert.attempts == MAX_ATTEMPTS


def test_redirects_are_not_followed(hook: Hook, tmp_path: Path) -> None:
    db = Database(MEMORY)
    raised(db)
    n = notifier(hook, db, tmp_path, Clock())
    hook.replies = [302]
    assert n.run_once(NOW) == 0
    assert [r["path"] for r in hook.received] == [SECRET_PATH]
    assert AlertStore(db).recent()[0].last_error == "HTTP 302"


def test_unreachable_and_logs_hold_no_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = Database(MEMORY)
    raised(db)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    token.chmod(0o600)
    dead = WebhookSettings(url=f"http://127.0.0.1:9{SECRET_PATH}", token_file=token)
    with caplog.at_level(logging.DEBUG):
        assert WebhookNotifier(dead, db, clock=Clock()).run_once(NOW) == 0
    assert AlertStore(db).recent()[0].last_error.startswith("network error")  # type: ignore[union-attr]
    assert TOKEN not in caplog.text and SECRET_PATH not in caplog.text
    assert SECRET_PATH not in repr(WebhookNotifier(dead, db))


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://ntfy.sh/openvigie", True),
        ("http://127.0.0.1:8080/hook", True),
        ("http://example.com/hook", False),
        ("https://user:pw@example.com/hook", False),
        ("ftp://example.com/hook", False),
    ],
)
def test_webhook_url_is_checked(url: str, ok: bool) -> None:
    if ok:
        assert HubSettings(alerts=AlertSettings(webhook=WebhookSettings(url=url)))
    else:
        with pytest.raises(ValidationError):
            WebhookSettings(url=url)


def test_off_by_default() -> None:
    assert HubSettings().alerts.webhook is None
