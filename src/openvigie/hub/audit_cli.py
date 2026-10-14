"""``openvigie hub audit verify|export``: check and export the audit log (FR-17, D14).

``verify`` walks the hash chain and prints the number of entries and the head hash. A chain
can't show that the newest entries were cut off, so keep the printed head (or an export)
somewhere else if you need to detect that.
"""

from __future__ import annotations

import json
import sys
from typing import TextIO

from openvigie.config import HubSettings
from openvigie.hub.store.audit import AuditLog
from openvigie.hub.store.db import Database


def run(settings: HubSettings, command: str, out: TextIO = sys.stdout) -> int:
    db = Database(settings.resolved_db_path())
    try:
        log = AuditLog(db)
        if command == "export":
            for entry in log.all():
                out.write(json.dumps(entry.to_json(), sort_keys=True) + "\n")
            return 0
        result = log.verify()
        if not result.ok:
            print(f"BROKEN at entry {result.broken_at}: {result.reason} "
                  f"({result.checked} entries checked before it)", file=out)  # fmt: skip
            return 1
        head = log.entries(limit=1)
        print(f"ok: {result.checked} entries, chain intact"
              + (f", head {head[0].hash}" if head else ""), file=out)  # fmt: skip
        return 0
    finally:
        db.close()
