"""Running the hub's commands on this host (D14), only the actions this host allows.

Offers ``CommandRunner.run_all``. A command is refused, and the refusal reported to the hub,
when its action is not in ``allow_actions`` in collector.toml (empty by default), when it has
expired, when its id was seen before, or when it is malformed. Seen ids are kept until the
command expires, in the state store when there is one, so a restart doesn't allow a replay.

No real action exists yet: ``noop`` does nothing, for tests and end-to-end checks. Real ones
(e.g. applying an OpenShell policy, #17) are added to ``ACTIONS``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from openvigie.collector.state import StateStore
from openvigie.model import CodecError, Command, CommandResult, CommandStatus, from_dict
from openvigie.redact import redact

log = logging.getLogger(__name__)

# an action returns a one-line detail for the hub, or raises to report a failure
Action = Callable[[Mapping[str, str]], str]


def noop(args: Mapping[str, str]) -> str:
    return "did nothing, as asked"


ACTIONS: dict[str, Action] = {"noop": noop}
MAX_DETAIL = 500


class CommandRunner:
    def __init__(
        self,
        allow: tuple[str, ...],
        store: StateStore | None = None,
        *,
        actions: Mapping[str, Action] = ACTIONS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.allow = frozenset(allow)
        self.store = store
        self.actions = actions
        self.clock = clock
        self.seen: dict[str, datetime] = {}  # used without a store

    def run_all(self, raw: object) -> list[CommandResult]:
        """Run the ``commands`` of an ingest reply, in order; one result per identifiable one."""
        if not isinstance(raw, list):
            return []
        results = []
        for item in raw[:16]:
            result = self.run(item)
            if result is not None:
                results.append(result)
        return results

    def run(self, item: Any) -> CommandResult | None:
        try:
            command = from_dict(Command, item)
        except CodecError as exc:
            command_id = item.get("command_id") if isinstance(item, dict) else None
            log.warning("refused a malformed command from the hub: %s", exc)
            if isinstance(command_id, str) and len(command_id) == 32:
                return self._result(command_id, CommandStatus.REFUSED, "malformed command")
            return None
        now = self.clock()
        if not self._first_sight(command, now):
            return self._refuse(command, "already received (replay)")
        if command.expires_at <= now:
            return self._refuse(command, "expired")
        action = self.actions.get(command.action)
        if command.action not in self.allow or action is None:
            return self._refuse(command, "action not allowed on this host (allow_actions)")
        log.warning("running %s for the hub (command %s)", command.action, command.command_id)
        try:
            detail = action(command.args)
        except Exception as exc:  # any failure is reported, never raised
            return self._result(command.command_id, CommandStatus.FAILED,
                                f"{type(exc).__name__}: {exc}")  # fmt: skip
        return self._result(command.command_id, CommandStatus.SUCCEEDED, detail)

    def _first_sight(self, command: Command, now: datetime) -> bool:
        if self.store is not None:
            return self.store.first_sight(command.command_id, command.expires_at, now)
        self.seen = {k: v for k, v in self.seen.items() if v >= now}
        if command.command_id in self.seen:
            return False
        self.seen[command.command_id] = command.expires_at
        return True

    def _refuse(self, command: Command, why: str) -> CommandResult:
        log.warning("refused %s from the hub (command %s): %s", command.action,
                    command.command_id, why)  # fmt: skip
        return self._result(command.command_id, CommandStatus.REFUSED, why)

    def _result(self, command_id: str, status: CommandStatus, detail: str) -> CommandResult:
        return CommandResult(command_id, status, self.clock(), redact(detail)[:MAX_DETAIL])
