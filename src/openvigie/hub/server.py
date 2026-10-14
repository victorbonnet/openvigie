"""Run the hub with uvicorn on the configured (loopback by default) address."""

from __future__ import annotations

import logging

import uvicorn

from openvigie.config import HubSettings
from openvigie.hub.app import create_app
from openvigie.hub.store.db import Database
from openvigie.hub.tailscale import advice, detect

log = logging.getLogger(__name__)


def serve(settings: HubSettings) -> None:
    db = Database(settings.resolved_db_path())
    app = create_app(settings, db)
    host = f"[{settings.bind}]" if ":" in settings.bind else settings.bind
    print(f"openvigie hub listening on http://{host}:{settings.port}")
    for line in advice(detect(), settings.port, settings.allowed_hosts):
        print(line)
    try:
        uvicorn.run(
            app,
            host=settings.bind,
            port=settings.port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
        )
    finally:
        db.close()
