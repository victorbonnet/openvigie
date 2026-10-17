"""Write-action routes (FR-17, D14): propose, review the diff, confirm, cancel, roll back,
and the network-rules editor of a sandbox (FR-12, D15).

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
from typing import Annotated, Literal
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Path, Query, Request
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
from openvigie.hub.roles import Admin, identity, refuse, same_origin
from openvigie.hub.store.commands import CommandQueue
from openvigie.hub.store.inventory import AgentRow, Inventory
from openvigie.hub.store.sandboxes import SandboxStore
from openvigie.hub.templating import render
from openvigie.hub.ui import starter_view
from openvigie.model import OpenShellSandbox
from openvigie.openshell import policy
from openvigie.openshell.policy import MAX_TEXT

router = APIRouter(include_in_schema=False)

MAX_FORM_BYTES = 4 * MAX_TEXT  # a policy text, URL-encoded
ActionId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Short = Annotated[str, Field(max_length=256)]


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    csrf: Annotated[str, Field(max_length=128)]


class ProposeForm(Form):
    kind: Annotated[str, Field(max_length=64)]
    # the parameters kinds take
    host_id: Short = ""
    workspace: Short = ""
    sandbox: Short = ""
    text: Annotated[str, Field(max_length=MAX_TEXT)] = ""
    agent_id: Short = ""


class ConfirmForm(Form):
    typed: Short = ""


def csrf_token(request: Request, purpose: str) -> str:
    secret: bytes = request.app.state.csrf_secret
    message = f"{identity(request).actor}\x1f{purpose}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


async def _form[F: Form](request: Request, model: type[F], purpose: str) -> F:
    if not same_origin(request):
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
def action_page(request: Request, action_id: ActionId) -> HTMLResponse:
    return _action_page(request, action_id)


def _action_page(request: Request, action_id: str, error: str | None = None) -> HTMLResponse:
    """The review page; ``error`` comes from a failed step, never from the URL."""
    action = _action_or_404(request, action_id)
    command = CommandQueue(request.app.state.db).get(action.command_id or "")
    kind = _kinds(request).get(action.kind)
    can_undo = kind is not None and action.status == "confirmed" and kind.undo(action) is not None
    page = render(request, "action.html", title=action.plan.summary, action=action,
                  status=action.current_status(datetime.now(UTC)), command=command,
                  diff=diff_lines(action.plan.before, action.plan.after), can_undo=can_undo,
                  review=kind.review(action.plan) if kind else [], error=error)  # fmt: skip
    if error:
        page.status_code = 400
    return page


@router.post("/actions/new", response_model=None)
async def new_action(request: Request, who: Admin) -> RedirectResponse | HTMLResponse:
    form = await _form(request, ProposeForm, "propose")
    params = form.model_dump(exclude={"csrf", "kind"})
    try:
        action = propose(request.app.state.db, _kinds(request), form.kind, params, who,
                         datetime.now(UTC))  # fmt: skip
    except ActionError as exc:
        box = _box(request, form.host_id, form.workspace, form.sandbox)
        if form.kind == "sandbox.policy" and box is not None:  # back to the editor, text kept
            return _editor(request, box, form.text, str(exc))
        raise HTTPException(400, str(exc)) from None
    return _see(action)


def _box(request: Request, host_id: str, workspace: str, name: str) -> OpenShellSandbox | None:
    boxes = SandboxStore(request.app.state.db).all(host_id) if host_id else []
    return next((b for b in boxes if (b.workspace, b.name) == (workspace, name)), None)


@router.get("/sandboxes/{sandbox_id}/rules", response_class=HTMLResponse)
def rules_page(
    request: Request,
    sandbox_id: Annotated[str, Path(max_length=256)],
    start: Annotated[Literal["current", "starter"], Query(alias="from")] = "current",
    allow: Annotated[str | None, Query(pattern=r"^[0-9a-f]{16}$")] = None,
) -> HTMLResponse:
    """A sandbox's network rules as YAML; admins can edit them and propose the change. With
    ``allow``, the current rules plus one allowing that blocked request (FR-18)."""
    store = SandboxStore(request.app.state.db)
    box = store.get(sandbox_id)
    if box is None:
        raise HTTPException(404, "sandbox not found")
    text = None
    if allow is not None:
        blocked = next((b for b in store.blocked(sandbox_id) if b.key == allow), None)
        if blocked is None:
            raise HTTPException(404, "blocked request not found")
        if box.network_policy is None:  # proposing from no rules would drop the unknown ones
            return _editor(request, box, error="Can't propose allowing it: this sandbox's "
                                               "current rules are unknown.")  # fmt: skip
        else:
            try:
                rules = policy.from_json(box.network_policy)
                text = policy.to_yaml(policy.allowing(rules, blocked.binary, blocked.host,
                                                      blocked.port))  # fmt: skip
            except policy.PolicyError:
                return _editor(request, box, error="This destination can't be written as a rule "
                                                   "here; add it by hand.")  # fmt: skip
    elif start == "starter" and (row := _agent_of(request, box)) is not None:
        rules = policy.parse_yaml(starter_view(request, row, box).policy.yaml).rules
        text = policy.to_yaml(rules)
    return _editor(request, box, text)


def _agent_of(request: Request, box: OpenShellSandbox) -> AgentRow | None:
    rows = Inventory(request.app.state.db).agents(host_id=box.host_id)
    return next((r for r in rows if r.agent.agent_id in box.agent_ids), None)


def _editor(request: Request, box: OpenShellSandbox, text: str | None = None,
            error: str | None = None) -> HTMLResponse:  # fmt: skip
    problem = None
    if text is None:
        if box.network_policy is None:
            problem = "The collector couldn't read this sandbox's rules from OpenShell."
        else:
            try:
                text = policy.to_yaml(policy.from_json(box.network_policy))
            except policy.PolicyError:
                problem = ("These rules use fields openvigie's editor doesn't support yet; "
                           "change them with `openshell policy set`.")  # fmt: skip
    page = render(request, "rules.html", title=f"Network rules of {box.name}", box=box,
                  text=text, problem=problem, error=error,
                  agent=_agent_of(request, box))  # fmt: skip
    if error:
        page.status_code = 400
    return page


@router.post("/actions/{action_id}/confirm", response_model=None)
async def confirm_action(
    request: Request, action_id: ActionId, who: Admin
) -> RedirectResponse | HTMLResponse:
    action = _action_or_404(request, action_id)
    form = await _form(request, ConfirmForm, f"decide:{action_id}")
    try:
        confirm(request.app.state.db, action, form.typed, who, datetime.now(UTC))
    except ActionError as exc:
        return _action_page(request, action_id, error=str(exc))
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
        return _action_page(request, action_id, error=str(exc))
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
        return _action_page(request, action_id, error=str(exc))
    return _see(undo)
