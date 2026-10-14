"""Jinja2 environment (autoescape on: agent data is untrusted) and the render helper."""

from __future__ import annotations

from typing import Any

import jinja2
from fastapi import Request
from fastapi.responses import HTMLResponse

from openvigie import __version__


def templates_env() -> jinja2.Environment:
    from openvigie.hub.ui import ago

    env = jinja2.Environment(
        loader=jinja2.PackageLoader("openvigie.hub", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
    env.globals["version"] = __version__
    env.filters["ago"] = ago
    return env


def render(request: Request, template: str, **context: Any) -> HTMLResponse:
    env: jinja2.Environment = request.app.state.templates
    return HTMLResponse(env.get_template(template).render(request=request, **context))
