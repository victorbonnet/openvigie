"""Write-action routes (FR-17, D14): propose, review the diff, confirm, cancel, roll back.

Offers ``router`` and ``csrf_token`` (a template global for forms). Pages are readable by every
role; each POST needs the admin role, a same-origin request and a CSRF token bound to the
admin's login and the form's purpose. Without that, a page an admin visits could post to the
hub through their browser, which Tailscale Serve would sign with the admin's identity. Bodies
are small URL-encoded forms, validated by the models below (D12).
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openvigie.hub.actions import (
    KINDS,
    ActionError,
    ActionStore,
    Kind,
    StoredAction,
    cancel,
    confirm,
    propose,
    rollback,
)
from openvigie.hub.roles import Admin, identity, refuse
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.templating import render

router = APIRouter(include_in_schema=False)

MAX_FORM_BYTES = 16 * 1024
ActionId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Short = Annotated[str, Field(max_length=256)]


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    csrf: Annotated[str, Field(max_length=128)]


class ProposeForm(Form):
    kind: Annotated[str, Field(max_length=64)]
    host_id: Short = ""  # the parameters kinds take; more come with new kinds


class ConfirmForm(Form):
    typed: Short = ""


def csrf_token(request: Request, purpose: str) -> str:
    secret: bytes = request.app.state.csrf_secret
    message = f"{identity(request).actor}\x1f{purpose}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _same_origin(request: Request) -> bool:
    """Browsers say where a POST comes from; tools like curl send neither header."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


async def _form[F: Form](request: Request, model: type[F], purpose: str) -> F:
    if not _same_origin(request):
        refuse(request, "cross-site request")
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415, "expected a form")
    body = await request.body()
    if len(body) > MAX_FORM_BYTES:
        raise HTTPException(413, "form too large")
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, max_num_fields=16)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("repeated field")
        form = model.model_validate(fields)
    except (ValueError, ValidationError):  # UnicodeDecodeError is a ValueError
        raise HTTPException(422, "invalid form") from None
    if not hmac.compare_digest(form.csrf, csrf_token(request, purpose)):
        refuse(request, "missing or stale form token; reload the page")
    return form


def _kinds(request: Request) -> dict[str, Kind]:
    return getattr(request.app.state, "action_kinds", KINDS)


def _action_or_404(request: Request, action_id: str) -> StoredAction:
    action = ActionStore(request.app.state.db).get(action_id)
    if action is None:
        raise HTTPException(404, "action not found")
    return action


def _see(action: StoredAction) -> RedirectResponse:
    return RedirectResponse(f"/actions/{action.action_id}", status_code=303)


def diff_lines(before: str, after: str) -> list[tuple[str, str]]:
    """(css class, line) of a unified diff, for the review page."""
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), "now", "after",
                                 lineterm="", n=3)  # fmt: skip
    classes = {"+": "add", "-": "del", "@": "hunk"}
    return [(classes.get(line[:1], ""), line) for line in list(lines)[2:]]


@router.get("/actions", response_class=HTMLResponse)
def actions_page(request: Request) -> HTMLResponse:
    actions = ActionStore(request.app.state.db).recent()
    return render(request, "actions.html", title="Actions", actions=actions,
                  now=datetime.now(UTC))  # fmt: skip


@router.get("/actions/{action_id}", response_class=HTMLResponse)
def action_page(request: Request, action_id: ActionId, error: str | None = None) -> HTMLResponse:
    action = _action_or_404(request, action_id)
    command = CommandQueue(request.app.state.db).get(action.command_id or "")
    kind = _kinds(request).get(action.kind)
    can_undo = kind is not None and action.status == "confirmed" and kind.undo(action) is not None
    page = render(request, "action.html", title=action.plan.summary, action=action,
                  status=action.current_status(datetime.now(UTC)), command=command,
                  diff=diff_lines(action.plan.before, action.plan.after), can_undo=can_undo,
                  error=error)  # fmt: skip
    if error:
        page.status_code = 400
    return page


@router.post("/actions/new")
async def new_action(request: Request, who: Admin) -> RedirectResponse:
    form = await _form(request, ProposeForm, "propose")
    params = form.model_dump(exclude={"csrf", "kind"})
    try:
        action = propose(request.app.state.db, _kinds(request), form.kind, params, who,
                         datetime.now(UTC))  # fmt: skip
    except ActionError as exc:
        raise HTTPException(400, str(exc)) from None
    return _see(action)


@router.post("/actions/{action_id}/confirm", response_model=None)
async def confirm_action(
    request: Request, action_id: ActionId, who: Admin
) -> RedirectResponse | HTMLResponse:
    action = _action_or_404(request, action_id)
    form = await _form(request, ConfirmForm, f"decide:{action_id}")
    try:
        confirm(request.app.state.db, action, form.typed, who, datetime.now(UTC))
    except ActionError as exc:
        return action_page(request, action_id, error=str(exc))
    return _see(action)


@router.post("/actions/{action_id}/cancel", response_model=None)
async def cancel_action(
    request: Request, action_id: ActionId, who: Admin
) -> RedirectResponse | HTMLResponse:
    action = _action_or_404(request, action_id)
    await _form(request, Form, f"decide:{action_id}")
    try:
        cancel(request.app.state.db, action, who, datetime.now(UTC))
    except ActionError as exc:
        return action_page(request, action_id, error=str(exc))
    return _see(action)


@router.post("/actions/{action_id}/rollback", response_model=None)
async def rollback_action(
    request: Request, action_id: ActionId, who: Admin
) -> RedirectResponse | HTMLResponse:
    action = _action_or_404(request, action_id)
    await _form(request, Form, f"rollback:{action_id}")
    try:
        undo = rollback(request.app.state.db, _kinds(request), action, who, datetime.now(UTC))
    except ActionError as exc:
        return action_page(request, action_id, error=str(exc))
    return _see(undo)
