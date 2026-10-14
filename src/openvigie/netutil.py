"""Outbound HTTP helpers shared by the collector and the hub (stdlib ``urllib``)."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would replay our ``Authorization`` header to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError with the 3xx status


def opener() -> urllib.request.OpenerDirector:
    """A urllib opener that never follows redirects."""
    return urllib.request.build_opener(_NoRedirect)


MAX_RESPONSE = 64 * 1024


def post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    """POST without following redirects; the HTTP status. Raises ``OSError`` on network failure.
    Callers check the URL's scheme when they load their settings."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with opener().open(request, timeout=timeout) as response:
            response.read(MAX_RESPONSE)
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
