"""``openvigie enrol <hub-url> <code>``: exchange a one-time code for this host's token."""

from __future__ import annotations

import re
from pathlib import Path

from openvigie.collector.client import HubClient, HubError, Transport, urllib_transport
from openvigie.config import (
    CollectorSettings,
    config_dir,
    ensure_private_dir,
    write_private_file,
)
from openvigie.model import Host, to_dict

HUB_URL_LINE = re.compile(r"(?m)^\s*hub_url\s*=.*$")


def save_hub_url(config_path: Path, url: str) -> None:
    """Set ``hub_url`` in collector.toml, keeping every other line."""
    line = f'hub_url = "{url}"'
    text = config_path.read_text() if config_path.exists() else ""
    found = HUB_URL_LINE.search(text)
    text = HUB_URL_LINE.sub(line, text, count=1) if found else f"{line}\n{text}"
    ensure_private_dir(config_path.parent)
    config_path.write_text(text)


def enrol(
    hub_url: str,
    code: str,
    host: Host,
    settings: CollectorSettings,
    *,
    config_path: Path | None = None,
    transport: Transport = urllib_transport,
) -> Path:
    client = HubClient(hub_url, transport=transport)
    reply = client.post("/api/v1/enrol", {"code": code.strip(), "host": to_dict(host)})
    token = reply.get("token")
    if not isinstance(token, str) or not token.startswith("ovh_") or len(token) > 128:
        raise HubError(0, "hub did not return a valid token")
    token_file = settings.resolved_token_file()
    write_private_file(token_file, token + "\n")
    save_hub_url(config_path or config_dir() / "collector.toml", client.base_url)
    return token_file
