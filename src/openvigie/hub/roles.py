"""Who makes a request and what they may do (FR-17, PRD D14).

Offers ``IdentityMiddleware`` (installed by ``create_app``), ``identity(request)``, and
``Admin``: annotate a write route's parameter with it to require the admin role.

Everyone is a ``viewer``. An ``admin`` is a login listed in ``hub.toml`` ``admins``, read from
the ``Tailscale-User-Login`` header that ``tailscale serve`` adds, and only when
``trust_tailscale_identity`` is on. Every request reaches the hub from loopback, so any local
user could send that header too: trusting it needs a single-user hub host (threat model T13).
Even then a request carries identity only when it comes from loopback and is addressed to a
non-loopback name (the MagicDNS name that Tailscale Serve forwards); a browser on 127.0.0.1 is
anonymous. Every ``Tailscale-*`` header is removed before any handler runs.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from openvigie.config import HubSettings, is_loopback
from openvigie.hub.store.audit import AuditLog

log = logging.getLogger(__name__)

IDENTITY_PREFIX = b"tailscale-"
LOGIN_HEADER = b"tailscale-user-login"
SCOPE_KEY = "openvigie.identity"
LOGIN = re.compile(r"[A-Za-z0-9._%+@:-]{1,256}")  # as config.Login
REFUSALS_PER_MINUTE = 30  # audited refusals; any web page can POST to 127.0.0.1


@dataclass(frozen=True)
class Identity:
    login: str | None  # None: anonymous
    role: Literal["viewer", "admin"]

    @property
    def actor(self) -> str:
        return self.login or "anonymous"


ANONYMOUS = Identity(None, "viewer")


def resolve(settings: HubSettings, client: str | None, host: str, login: str | None) -> Identity:
    """The identity of one request, from its peer address, Host header and login header."""
    if not (settings.trust_tailscale_identity and login and client and is_loopback(client)):
        return ANONYMOUS
    if is_loopback(_hostname(host)) or not LOGIN.fullmatch(login):
        return ANONYMOUS
    return Identity(login, "admin" if login in settings.admins else "viewer")


def _hostname(host: str) -> str:
    if host.startswith("["):  # [::1]:7878
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class IdentityMiddleware:
    """Resolve the identity once, store it in the scope, then drop every ``Tailscale-*`` header."""

    def __init__(self, app: ASGIApp, settings: HubSettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = scope.get("headers", [])
            values = dict(headers)
            login = values.get(LOGIN_HEADER, b"").decode("latin-1") or None
            host = values.get(b"host", b"").decode("latin-1")
            client = scope["client"][0] if scope.get("client") else None
            scope = {
                **scope,
                "headers": [(k, v) for k, v in headers if not k.startswith(IDENTITY_PREFIX)],
                SCOPE_KEY: resolve(self.settings, client, host, login),
            }
        await self.app(scope, receive, send)


def identity(request: Request) -> Identity:
    found = request.scope.get(SCOPE_KEY)
    return found if isinstance(found, Identity) else ANONYMOUS


class RefusalBudget:
    """At most ``per_minute`` refusals reach the audit log, which is kept forever."""

    def __init__(self, per_minute: int = REFUSALS_PER_MINUTE) -> None:
        self.per_minute = per_minute
        self.minute = 0
        self.used = 0

    def take(self, now: float) -> bool:
        minute = int(now // 60)
        if minute != self.minute:
            self.minute, self.used = minute, 0
        self.used += 1
        return self.used <= self.per_minute


def require_admin(request: Request) -> Identity:
    """FastAPI dependency for write routes: admins pass, anyone else gets a 403 (audited)."""
    who = identity(request)
    if who.role == "admin":
        return who
    budget: RefusalBudget = request.app.state.refusals
    if budget.take(time.monotonic()):
        AuditLog(request.app.state.db).record(
            at=datetime.now(UTC), actor=who.actor, role=who.role,
            action=f"{request.method} {request.url.path}", target=request.url.path,
            result="refused", request_id=uuid.uuid4().hex,
        )  # fmt: skip
    else:
        log.warning("refused %s %s for %s (not audited: too many refusals this minute)",
                    request.method, request.url.path, who.actor)  # fmt: skip
    raise HTTPException(403, "admin role required")


Admin = Annotated[Identity, Depends(require_admin)]
