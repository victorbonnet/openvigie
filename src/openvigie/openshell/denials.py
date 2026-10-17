"""Network requests a sandbox blocked (FR-18), read from its OpenShell log.

Offers ``parse`` (log text → ``Denial`` per blocked connection) and ``Tally`` (new denials per
sandbox since the last read, grouped by program and destination).

OpenShell 0.1.4 has no API for denials. Its sandbox log has one OCSF line per blocked connection:
``[<epoch>] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED <program>(<pid>) -> <host>:<port>
[reason:<reason>]``. Only these lines are read: the proxy's own warning about the same connection
carries the requesting process's command line, which can hold secrets.

The gateway keeps only the last few dozen lines per sandbox, so a busy sandbox can block more
than a read sees: counts are a lower bound. Lines already counted are skipped by timestamp.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

MAX_LINES = 5000
LINE = re.compile(
    r"^\[(?P<at>\d{1,12}(?:\.\d{1,9})?)\] \[sandbox\] \[OCSF *\] \[ocsf\] NET:OPEN \[[A-Z]{1,8}\] "
    r"DENIED (?P<binary>/[^\s()]{0,1023})\(\d{1,10}\) -> (?P<host>[^\s/]{1,255}):(?P<port>\d{1,5}) "
    r"\[reason:(?P<reason>[a-z0-9_]{1,64})\]$"
)
HOST = re.compile(r"^(?:[A-Za-z0-9*_][A-Za-z0-9*_.-]{0,252}|\[[0-9A-Fa-f:.]{2,45}\])$")


@dataclass(frozen=True)
class Denial:
    at: float  # seconds since the epoch, as OpenShell logs it
    binary: str
    host: str
    port: int
    reason: str


def parse(text: str) -> list[Denial]:
    """Every blocked connection in ``text``, oldest first; any other line is ignored."""
    found = []
    for line in text.splitlines()[-MAX_LINES:]:
        m = LINE.match(line)
        if m and HOST.match(m["host"]) and 0 < int(m["port"]) <= 65535:
            found.append(Denial(float(m["at"]), m["binary"], m["host"], int(m["port"]),
                                m["reason"]))  # fmt: skip
    return found


Key = tuple[str, str, int]  # program, host, port


@dataclass
class Group:
    count: int = 0
    last_seen: float = 0.0
    reason: str = ""


@dataclass
class Tally:
    """Denials not yet reported, per sandbox, and the newest line already counted per sandbox."""

    seen: dict[str, float] = field(default_factory=dict)  # sandbox id → newest timestamp counted
    pending: dict[str, dict[Key, Group]] = field(default_factory=dict)

    def add(self, sandbox_id: str, denials: Iterable[Denial]) -> None:
        after = self.seen.get(sandbox_id, 0.0)
        groups = self.pending.setdefault(sandbox_id, {})
        for d in denials:
            if d.at <= after:
                continue
            group = groups.setdefault((d.binary, d.host, d.port), Group())
            group.count += 1
            group.last_seen = max(group.last_seen, d.at)
            group.reason = d.reason
            self.seen[sandbox_id] = max(self.seen.get(sandbox_id, 0.0), d.at)
        if not groups:
            del self.pending[sandbox_id]

    def forget(self, alive: set[str]) -> None:
        """Drop sandboxes that no longer exist."""
        self.seen = {k: v for k, v in self.seen.items() if k in alive}
        self.pending = {k: v for k, v in self.pending.items() if k in alive}
