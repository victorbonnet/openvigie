"""Outbound HTTP helpers shared by the collector and the hub (stdlib ``urllib``)."""

from __future__ import annotations

import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would replay our ``Authorization`` header to the new host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError with the 3xx status


def opener() -> urllib.request.OpenerDirector:
    """A urllib opener that never follows redirects."""
    return urllib.request.build_opener(_NoRedirect)
