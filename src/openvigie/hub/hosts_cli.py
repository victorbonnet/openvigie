"""``openvigie hub hosts list|revoke|rotate``: local admin commands on the hub database.

They run on the hub machine against its database directly, so P0 still has no web write path.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

from openvigie.config import HubSettings
from openvigie.hub.store.credentials import Credentials
from openvigie.hub.store.db import Database
from openvigie.hub.store.inventory import HostRow, Inventory


def _when(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


def resolve(inventory: Inventory, ident: str) -> HostRow | None:
    """Match a host by id, id prefix (8+ chars) or unique hostname."""
    hosts = inventory.hosts()
    exact = [h for h in hosts if h.host.host_id == ident]
    if exact:
        return exact[0]
    matches = [
        h for h in hosts
        if (len(ident) >= 8 and h.host.host_id.startswith(ident)) or h.host.hostname == ident
    ]  # fmt: skip
    return matches[0] if len(matches) == 1 else None


def run(settings: HubSettings, command: str, ident: str | None, ttl: timedelta) -> int:
    db = Database(settings.resolved_db_path())
    try:
        inventory, creds = Inventory(db), Credentials(db)
        now = datetime.now(UTC)
        if command == "list":
            tokens = creds.token_info()
            print(f"{'HOST ID':34} {'HOSTNAME':20} {'LAST SEEN':17} {'TOKEN USED':17} STATE")
            for row in inventory.hosts():
                created, used = tokens.get(row.host.host_id, (None, None))
                state = "revoked" if row.revoked_at else ("active" if created else "no token")
                print(f"{row.host.host_id:34} {row.host.hostname[:20]:20} "
                      f"{_when(row.last_seen):17} {_when(used):17} {state}")  # fmt: skip
            return 0
        target = resolve(inventory, ident or "")
        if target is None:
            print(f"openvigie: no single host matches {ident!r}", file=sys.stderr)
            return 1
        host_id, hostname = target.host.host_id, target.host.hostname
        if command == "revoke":
            creds.revoke_host(host_id, now)
            print(f"Revoked {hostname} ({host_id}). Its data is kept; its collector "
                  "is refused from now on.")  # fmt: skip
            return 0
        code = creds.create_enrol_code(now, ttl, bound_host_id=host_id)
        minutes = int(ttl.total_seconds() // 60)
        print(f"Re-enrolment code for {hostname} (valid {minutes} min, only for this host):")
        print(f"\n  {code}\n\nOn that host: openvigie enrol <hub-url> {code}")
        return 0
    finally:
        db.close()
