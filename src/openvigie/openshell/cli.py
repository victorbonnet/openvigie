"""Read OpenShell sandboxes and their policies through the ``openshell`` CLI (FR-10, PRD D13).

Only the read-only commands below are ever run: an argv list (never a shell), a minimal
environment, a timeout and bounded output. Names from OpenShell are checked before they are used
as arguments. ``read_inventory`` never raises: problems come back as ``Inventory.state``.

Cost: one ``sandbox list`` per call. ``sandbox get`` runs again only when a sandbox's
``resource_version`` changes, and ``policy get`` only when its policy hash changes.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from openvigie.model import AdapterHealth, HealthState
from openvigie.openshell.models import Policy, Sandbox, SandboxPage
from openvigie.openshell.versions import MIN_SUPPORTED, is_supported
from openvigie.redact import redact

TIMEOUT_S = 15.0
MAX_OUTPUT = 4 * 1024 * 1024
MAX_PAGES = 10
PAGE_SIZE = 100
# Sandbox and workspace names as OpenShell creates them; never starts with "-" (no flag injection)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# What the CLI needs to find its gateway registration and mTLS material, and nothing else
ENV_KEEP = ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
            "OPENSHELL_GATEWAY", "OPENSHELL_WORKSPACE")  # fmt: skip

_ERROR_PREFIX = re.compile(r"^\s*(?:Error:)?[\s\u00d7]*")  # strips the CLI's "Error:  x " marker

State = Literal["ok", "not_installed", "unsupported", "unreachable", "error"]
Run = Callable[[Sequence[str]], str]  # CLI arguments → stdout; raises CommandError


class CommandError(Exception):
    """A CLI call failed. The message is bounded and redacted."""


@dataclass(frozen=True)
class SandboxView:
    sandbox: Sandbox
    policy: Policy | None  # None when it could not be read


@dataclass(frozen=True)
class Inventory:
    state: State
    reason: str | None = None
    version: str | None = None
    sandboxes: tuple[SandboxView, ...] = ()


def cli_runner(binary: str | None = None, timeout: float = TIMEOUT_S) -> Run | None:
    """A runner for the installed ``openshell``, or ``None`` when it isn't installed."""
    path = binary or shutil.which("openshell")
    if path is None:
        return None
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP} | {"NO_COLOR": "1"}

    def run(args: Sequence[str]) -> str:
        return _bounded_run([path, *args], env, timeout)

    return run


def _bounded_run(argv: list[str], env: dict[str, str], timeout: float) -> str:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # noqa: S603
                            stderr=subprocess.PIPE, env=env)  # fmt: skip
    if proc.stdout is None or proc.stderr is None:  # can't happen with PIPE; keeps types exact
        raise CommandError("no output pipes")
    chunks: dict[int, list[bytes]] = {proc.stdout.fileno(): [], proc.stderr.fileno(): []}
    size, deadline = 0, time.monotonic() + timeout
    with selectors.DefaultSelector() as sel:
        sel.register(proc.stdout, selectors.EVENT_READ)
        sel.register(proc.stderr, selectors.EVENT_READ)
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0 or size > MAX_OUTPUT:
                proc.kill()
                proc.wait()
                raise CommandError("timed out" if left <= 0 else "output too large")
            for key, _ in sel.select(left):
                data = os.read(key.fd, 65536)
                if not data:
                    sel.unregister(key.fileobj)
                chunks[key.fd].append(data)
                size += len(data)
    code = proc.wait()
    out = b"".join(chunks[proc.stdout.fileno()]).decode("utf-8", "replace")
    if code != 0:
        err = b"".join(chunks[proc.stderr.fileno()]).decode("utf-8", "replace")
        first = next((_ERROR_PREFIX.sub("", ln) for ln in err.splitlines() if ln.strip()), "")
        raise CommandError(f"exit {code}: {redact(first)[:200] or 'no message'}")
    return out


@dataclass
class Reader:
    """Reads the inventory, remembering sandbox details and policies between calls."""

    run: Run | None
    _details: dict[str, tuple[int | None, Sandbox]] = field(default_factory=dict)
    _policies: dict[tuple[str, str], Policy] = field(default_factory=dict)

    def read(self) -> Inventory:
        if self.run is None:
            return Inventory("not_installed")
        try:
            version = _version(self.run(["--version"]))
        except CommandError as exc:
            return Inventory("error", f"openshell --version failed ({exc})")
        if version is None or not is_supported(version):
            reason = (
                f"OpenShell {version or '?'} is not supported; install {MIN_SUPPORTED} or newer"
            )
            return Inventory("unsupported", reason, version)
        try:
            listed = _list(self.run)
        except CommandError as exc:
            return Inventory("unreachable", f"can't list sandboxes ({exc})", version)
        except ValidationError:
            return Inventory("error", "unexpected `sandbox list` output", version)
        run = self.run
        views = tuple(self._view(run, s) for s in listed if _safe(s))
        self._forget({s.id for s in listed})
        return Inventory("ok", None, version, views)

    def _view(self, run: Run, listed: Sandbox) -> SandboxView:
        sandbox = self._detail(run, listed)
        admission = sandbox.configuration_admission
        policy_key = (sandbox.id, admission.policy_hash if admission else "")
        policy = self._policies.get(policy_key)
        if policy is None or not policy_key[1]:
            policy = _get(run, Policy, "policy", "get", sandbox, "--base")
            if policy is not None:  # a failed read is retried on the next call
                self._policies[policy_key] = policy
        return SandboxView(sandbox, policy)

    def _detail(self, run: Run, listed: Sandbox) -> Sandbox:
        cached = self._details.get(listed.id)
        if cached and cached[0] == listed.resource_version and listed.resource_version is not None:
            return cached[1]
        detail = _get(run, Sandbox, "sandbox", "get", listed)
        if detail is None:
            return listed  # retried on the next call
        self._details[listed.id] = (listed.resource_version, detail)
        return detail

    def _forget(self, alive: set[str]) -> None:
        self._details = {k: v for k, v in self._details.items() if k in alive}
        self._policies = {k: v for k, v in self._policies.items() if k[0] in alive}


def _list(run: Run) -> list[Sandbox]:
    found: list[Sandbox] = []
    token = ""
    for _ in range(MAX_PAGES):
        args = ["sandbox", "list", "--all-workspaces", "-o", "json", "--page-size", str(PAGE_SIZE)]
        if token:
            args += ["--page-token", token]
        page = SandboxPage.model_validate_json(run(args))
        found += page.sandboxes
        token = page.next_page_token
        if not token:
            break
    return found


def _get[M: (Sandbox, Policy)](run: Run, model: type[M], group: str, verb: str, sandbox: Sandbox,
                           *flags: str) -> M | None:  # fmt: skip
    args = [group, verb, sandbox.name, "--workspace", sandbox.workspace, *flags, "-o", "json"]
    try:
        return model.model_validate_json(run(args))
    except (CommandError, ValidationError):
        return None


def inventory_health(inventory: Inventory) -> AdapterHealth | None:
    """An ``openshell`` row for the host's health list; none when OpenShell isn't installed."""
    if inventory.state == "not_installed":
        return None
    if inventory.state == "ok":
        return AdapterHealth("openshell", HealthState.OK)
    return AdapterHealth("openshell", HealthState.DEGRADED, inventory.reason)


def _version(output: str) -> str | None:
    """``openshell 0.1.4`` → ``0.1.4``."""
    match = re.fullmatch(r"openshell\s+v?(\S{1,32})", output.strip())
    return match[1] if match else None


def _safe(sandbox: Sandbox) -> bool:
    return bool(SAFE_NAME.match(sandbox.name) and SAFE_NAME.match(sandbox.workspace))
