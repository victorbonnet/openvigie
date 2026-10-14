"""The hub web app: hardened FastAPI skeleton that the API and UI plug into.

Defences that apply to every route:
* ``TrustedHostMiddleware``: a hostile web page can't use DNS rebinding to read the hub
  through ``127.0.0.1``.
* Strict CSP and related headers, no inline scripts, vendored assets only.
* Jinja2 autoescape (agent names and versions come from untrusted files).
* A request body limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from importlib import resources
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openvigie import __version__
from openvigie.config import ConfigError, HubSettings
from openvigie.hub import api, ui
from openvigie.hub.retention import retention_lifespan
from openvigie.hub.store.db import Database
from openvigie.hub.store.inventory import Inventory
from openvigie.hub.tailscale import StripIdentityHeaders
from openvigie.hub.templating import render, templates_env
from openvigie.pricing import load_price_table

MAX_BODY_BYTES = 2 * 1024 * 1024
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cache-control", b"no-store"),
]


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS if k not in names]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimit:
    """Reject request bodies over ``limit`` bytes, declared or streamed."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.limit:
                await _too_large(scope, receive, send)
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"detail": "request body too large"}, status_code=413)
    await response(scope, receive, send)


def display_timezone(settings: HubSettings) -> tzinfo:
    """Days are bucketed in this zone: the configured IANA name, else the hub's local zone."""
    if settings.display_timezone:
        try:
            return ZoneInfo(settings.display_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ConfigError(f"unknown display_timezone {settings.display_timezone!r}") from None
    return datetime.now().astimezone().tzinfo or UTC


def create_app(settings: HubSettings, db: Database, *, debug: bool = False) -> FastAPI:
    app = FastAPI(
        title="openvigie hub",
        version=__version__,
        docs_url="/docs" if debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if debug else None,
        lifespan=retention_lifespan,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.inventory = Inventory(db)
    app.state.prices = load_price_table()
    app.state.tz = display_timezone(settings)
    app.state.templates = templates_env()
    app.state.enrol_limiter = api.FailureLimiter()

    app.add_middleware(StripIdentityHeaders)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(SecurityHeaders)

    app.include_router(api.router)
    app.include_router(ui.router)
    static = resources.files("openvigie.hub") / "static"
    app.mount("/static", StaticFiles(directory=str(static)), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": exc.detail}, exc.status_code, headers=exc.headers)
        page = render(request, "error.html", title="Not found" if exc.status_code == 404
                      else "Error", status=exc.status_code, detail=str(exc.detail))  # fmt: skip
        page.status_code = exc.status_code
        return page

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
