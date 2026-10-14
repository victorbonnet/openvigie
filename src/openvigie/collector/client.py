"""Minimal HTTP client from collector to hub, on stdlib ``urllib`` (no extra dependency).

Plain ``http://`` is only allowed to a loopback hub; anything else must be ``https://`` with
certificate verification (e.g. ``tailscale serve``), so tokens never travel in clear text.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from openvigie.config import is_loopback

MAX_RESPONSE = 1024 * 1024
Transport = Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, bytes]]


class HubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"hub returned {status}: {message}" if status else message)
        self.status = status


def check_hub_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url!r}")
    if parts.username or parts.password:
        raise ValueError("credentials in the hub URL are not allowed")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ValueError(
            "plain http is only allowed to a hub on this machine; use the https:// URL "
            "from `tailscale serve` for a remote hub"
        )
    return url.rstrip("/")


def urllib_transport(
    method: str, url: str, body: bytes | None, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 - scheme checked
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(MAX_RESPONSE)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HubError(0, f"cannot reach the hub: {getattr(exc, 'reason', exc)}") from None


@dataclass
class HubClient:
    base_url: str
    token: str | None = None
    timeout: float = 10.0
    transport: Transport = urllib_transport

    def __post_init__(self) -> None:
        self.base_url = check_hub_url(self.base_url)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"content-type": "application/json", "user-agent": "openvigie-collector"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        body = json.dumps(payload, separators=(",", ":")).encode()
        status, raw = self.transport("POST", self.base_url + path, body, headers, self.timeout)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not 200 <= status < 300:
            detail = data.get("detail") if isinstance(data, dict) else None
            raise HubError(status, str(detail or "unexpected response")[:300])
        if not isinstance(data, dict):
            raise HubError(status, "unexpected response")
        return data
