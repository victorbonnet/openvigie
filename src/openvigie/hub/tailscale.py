"""Remote access through ``tailscale serve`` (decision D6): the hub itself stays on loopback.

``tailscale serve`` terminates HTTPS with the node's MagicDNS certificate and proxies to
``http://127.0.0.1:<port>``. The hub only has to accept that MagicDNS name in the ``Host``
header (``allowed_hosts``). Tailscale identity headers are stripped in P0: nothing may trust
them until roles exist (#12), and then only for requests arriving from the local proxy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

IDENTITY_PREFIX = b"tailscale-"


@dataclass(frozen=True)
class TailscaleInfo:
    dns_name: str  # e.g. "hub.tail1234.ts.net"

    def serve_command(self, port: int) -> str:
        return f"tailscale serve --bg --https=443 http://127.0.0.1:{port}"

    @property
    def url(self) -> str:
        return f"https://{self.dns_name}"


Runner = Callable[[list[str]], str]


def _run(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=3, check=True)  # noqa: S603 - fixed argv, no shell
    return result.stdout


def detect(
    runner: Runner = _run, which: Callable[[str], str | None] = shutil.which
) -> TailscaleInfo | None:
    """This node's MagicDNS name, or None when Tailscale isn't installed or running."""
    binary = which("tailscale")
    if not binary:
        return None
    try:
        status = json.loads(runner([binary, "status", "--json"])[: 2 * 1024 * 1024])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    me = status.get("Self") if isinstance(status, dict) else None
    name = me.get("DNSName") if isinstance(me, dict) else None
    if not isinstance(name, str) or not name.strip(".") or len(name) > 253:
        return None
    return TailscaleInfo(name.rstrip("."))


def advice(info: TailscaleInfo | None, port: int, allowed_hosts: tuple[str, ...]) -> list[str]:
    """Lines printed by ``openvigie hub serve`` to explain remote access."""
    if info is None:
        return ["Remote access: install Tailscale, then see docs/remote-access.md."]
    lines = [
        f"Remote access over Tailscale: run `{info.serve_command(port)}`",
        f"  then open {info.url} and enrol remote collectors with that URL.",
    ]
    if info.dns_name not in allowed_hosts:
        lines.append(
            f'  Add it to hub.toml first: allowed_hosts = ["127.0.0.1", "localhost", '
            f'"{info.dns_name}"]'
        )
    return lines


class StripIdentityHeaders:
    """Drop ``Tailscale-*`` request headers so no P0 code can come to rely on them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = [
                (k, v) for k, v in scope.get("headers", []) if not k.startswith(IDENTITY_PREFIX)
            ]
            scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)
