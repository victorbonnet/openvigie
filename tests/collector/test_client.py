import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from openvigie.collector.client import HubClient, HubError


class Server:
    """Answers every POST with a redirect to ``target``; records what each server saw."""

    def __init__(self, status: int = 200, location: str | None = None) -> None:
        self.seen: list[dict[str, str]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                server.seen.append(dict(self.headers))
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = _reply

            def log_message(self, *args: Any) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def target() -> Iterator[Server]:
    server = Server()
    yield server
    server.close()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_not_followed_with_the_token(target: Server, status: int) -> None:
    redirector = Server(status, location=target.url + "/steal")
    try:
        client = HubClient(redirector.url, token="ovh_secret")  # noqa: S106 - test value
        with pytest.raises(HubError) as raised:
            client.post("/api/v1/ingest", {})
        assert raised.value.status == status
        assert target.seen == []  # the token never reached the other host
    finally:
        redirector.close()


def test_plain_post_works(target: Server) -> None:
    assert HubClient(target.url, token="t").post("/x", {}) == {}  # noqa: S106
    assert target.seen[0]["Authorization"] == "Bearer t"
