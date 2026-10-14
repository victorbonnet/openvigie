"""Sending alerts to a webhook (FR-14, PRD D17). Off unless ``[alerts.webhook]`` is configured.

Offers ``WebhookNotifier.run_once`` (deliver what is pending) and ``notify_lifespan`` (do it
every ``INTERVAL_S`` inside the hub). Each alert is POSTed once as a small JSON object, with
``text`` and ``content`` set too so Slack and Discord incoming webhooks show it as is. A failed
delivery is retried with growing pauses, ``MAX_ATTEMPTS`` times in all; the last error shows on
the Budgets page. Redirects are never followed, and neither the URL (it can be a secret) nor
the token is logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI

from openvigie.config import ConfigError, WebhookSettings
from openvigie.hub.store.alerts import Alert, AlertStore
from openvigie.hub.store.db import Database
from openvigie.netutil import post

log = logging.getLogger(__name__)

INTERVAL_S = 15.0
TIMEOUT_S = 10.0
MAX_ATTEMPTS = 5
FIRST_PAUSE_S = 30.0

# (url, body, headers, timeout) -> HTTP status; raises OSError on network failure
Transport = Callable[[str, bytes, dict[str, str], float], int]


def payload(alert: Alert) -> dict[str, object]:
    text = f"[openvigie] {alert.title}: {alert.detail}"
    return {"source": "openvigie", "id": alert.id, "at": alert.at.isoformat(),
            "level": alert.level, "title": alert.title, "detail": alert.detail,
            "text": text, "content": text}  # fmt: skip


class WebhookNotifier:
    def __init__(self, settings: WebhookSettings, db: Database, transport: Transport = post,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.settings = settings
        self.store = AlertStore(db)
        self.transport = transport
        self.clock = clock
        self.retry_at: dict[int, float] = {}  # alert id -> earliest next attempt

    def __repr__(self) -> str:  # never show the URL or the token
        return "WebhookNotifier()"

    def run_once(self, now: datetime | None = None) -> int:
        """Deliver due alerts, oldest first; how many were delivered."""
        headers = {"Content-Type": "application/json", "User-Agent": "openvigie"}
        try:
            token = self.settings.token()
        except (ConfigError, OSError) as exc:
            log.error("alerts webhook: can't read token_file (%s)", exc)
            return 0
        if token:
            headers["Authorization"] = f"Bearer {token}"
        sent = 0
        for alert in self.store.pending(MAX_ATTEMPTS):
            if self.retry_at.get(alert.id, 0.0) > self.clock():
                continue
            error = self._send(alert, headers)
            if error is None:
                self.store.delivered(alert.id, now or datetime.now(UTC))
                self.retry_at.pop(alert.id, None)
                sent += 1
                continue
            self.store.failed(alert.id, error)
            pause = FIRST_PAUSE_S * 2**alert.attempts
            self.retry_at[alert.id] = self.clock() + pause
            log.warning("alerts webhook: alert %d not delivered (%s), attempt %d of %d", alert.id,
                        error, alert.attempts + 1, MAX_ATTEMPTS)  # fmt: skip
        return sent

    def _send(self, alert: Alert, headers: dict[str, str]) -> str | None:
        body = json.dumps(payload(alert)).encode()
        try:
            status = self.transport(self.settings.url, body, headers, TIMEOUT_S)
        except OSError as exc:
            return f"network error ({type(exc).__name__})"
        return None if 200 <= status < 300 else f"HTTP {status}"


@asynccontextmanager
async def notify_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Deliver alerts every ``INTERVAL_S`` when a webhook is configured; otherwise do nothing."""
    settings = app.state.settings.alerts.webhook
    if settings is None:
        yield
        return
    notifier = WebhookNotifier(settings, app.state.db)

    async def loop() -> None:
        while True:
            try:
                await asyncio.to_thread(notifier.run_once)
            except Exception:
                log.exception("alerts webhook run failed")
            await asyncio.sleep(INTERVAL_S)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
