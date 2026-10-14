"""The single definition of agent status (FR-2), shared by ``scan``, the collector and the UI.

| process running | activity within window | status  |
| --------------- | ---------------------- | ------- |
| any             | yes                    | active  |
| yes             | no                     | idle    |
| no              | no / unknown           | stopped |
| unknown         | no / unknown           | unknown |
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from openvigie.model import AgentInstance, AgentStatus, StatusEvent

SKEW_TOLERANCE = timedelta(seconds=60)


def compute_status(
    process_running: bool | None,
    last_activity_at: datetime | None,
    now: datetime,
    active_window: timedelta,
) -> AgentStatus:
    if last_activity_at is not None:
        age = now - min(last_activity_at, now)  # future timestamps (clock skew) count as now
        if age <= active_window:
            return AgentStatus.ACTIVE
    if process_running is True:
        return AgentStatus.IDLE
    if process_running is False:
        return AgentStatus.STOPPED
    return AgentStatus.UNKNOWN


@dataclass
class StatusTracker:
    """Applies ``compute_status`` across ticks and emits one event per real transition.

    Leaving ``active`` uses the window plus one tick of grace, so an agent sitting right at the
    window edge doesn't flap between active and idle.
    """

    active_window: timedelta
    grace: timedelta = timedelta(seconds=5)
    current: dict[str, AgentStatus] = field(default_factory=dict)
    skewed: set[str] = field(default_factory=set)

    def observe(
        self, agents: Iterable[AgentInstance], now: datetime
    ) -> tuple[list[AgentInstance], list[StatusEvent]]:
        updated: list[AgentInstance] = []
        events: list[StatusEvent] = []
        seen: set[str] = set()
        for agent in agents:
            seen.add(agent.agent_id)
            previous = self.current.get(agent.agent_id, AgentStatus.UNKNOWN)
            window = self.active_window
            if previous is AgentStatus.ACTIVE:
                window += self.grace
            if agent.last_activity_at and agent.last_activity_at > now + SKEW_TOLERANCE:
                self.skewed.add(agent.agent_id)
            else:
                self.skewed.discard(agent.agent_id)
            status = compute_status(agent.process_running, agent.last_activity_at, now, window)
            updated.append(dataclasses.replace(agent, status=status))
            if status is not previous or agent.agent_id not in self.current:
                events.append(StatusEvent(agent.agent_id, previous, status, now))
            self.current[agent.agent_id] = status
        for gone in sorted(set(self.current) - seen):
            if self.current[gone] is not AgentStatus.STOPPED:
                events.append(StatusEvent(gone, self.current[gone], AgentStatus.STOPPED, now))
            del self.current[gone]
        return updated, events
