"""Jinja2 environment (autoescape on: agent data is untrusted) and the render helper."""

from __future__ import annotations

from typing import Any

import jinja2
from fastapi import Request
from fastapi.responses import HTMLResponse

from openvigie import __version__


def templates_env() -> jinja2.Environment:
    from openvigie.hub.admin import csrf_token
    from openvigie.hub.roles import identity
    from openvigie.hub.ui import ago

    env = jinja2.Environment(
        loader=jinja2.PackageLoader("openvigie.hub", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.globals["version"] = __version__
    env.filters["ago"] = ago
    env.globals["adapter_alerts"] = adapter_alerts
    env.globals["whoami"] = identity
    env.globals["recent_alerts"] = recent_alerts
    env.globals["csrf_token"] = csrf_token
    return env


def adapter_alerts(request: Request) -> int:
    """Number of adapters, across hosts, that are degraded or failing (nav badge)."""
    from openvigie.model import HealthState

    health = request.app.state.inventory.adapter_health()
    return sum(h.health.state is not HealthState.OK for h in health)


def recent_alerts(request: Request) -> int:
    """Alerts raised in the last 24 hours (nav badge)."""
    from datetime import UTC, datetime, timedelta

    from openvigie.hub.store.alerts import AlertStore

    return AlertStore(request.app.state.db).count_since(datetime.now(UTC) - timedelta(days=1))


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    env: jinja2.Environment = request.app.state.templates
    return HTMLResponse(env.get_template(template).render(request=request, **context))
