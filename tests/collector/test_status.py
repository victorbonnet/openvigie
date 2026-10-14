from datetime import UTC, datetime, timedelta

import pytest

from openvigie.collector.status import StatusTracker, compute_status
from openvigie.model import AgentInstance, AgentStatus

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=5)
RECENT = NOW - timedelta(minutes=1)
OLD = NOW - timedelta(hours=1)

A, I, S, U = AgentStatus.ACTIVE, AgentStatus.IDLE, AgentStatus.STOPPED, AgentStatus.UNKNOWN  # noqa: E741


@pytest.mark.parametrize(
    ("running", "activity", "expected"),
    [
        (True, RECENT, A),
        (False, RECENT, A),
        (None, RECENT, A),
        (True, OLD, I),
        (True, None, I),
        (False, OLD, S),
        (False, None, S),
        (None, OLD, U),
        (None, None, U),
    ],
)
def test_status_matrix(
    running: bool | None, activity: datetime | None, expected: AgentStatus
) -> None:
    assert compute_status(running, activity, NOW, WINDOW) is expected


def test_window_edges() -> None:
    assert compute_status(True, NOW - WINDOW, NOW, WINDOW) is A
    assert compute_status(True, NOW - WINDOW - timedelta(seconds=1), NOW, WINDOW) is I


def test_future_activity_counts_as_now() -> None:
    assert compute_status(False, NOW + timedelta(hours=2), NOW, WINDOW) is A


def agent(running: bool | None, activity: datetime | None, agent_id: str = "a1") -> AgentInstance:
    return AgentInstance(agent_id, "h", "codex", "Codex", process_running=running,
                         last_activity_at=activity)  # fmt: skip


def test_tracker_emits_once_per_transition() -> None:
    tracker = StatusTracker(WINDOW)
    updated, events = tracker.observe([agent(True, RECENT)], NOW)
    assert updated[0].status is A
    assert [(e.old, e.new) for e in events] == [(U, A)]
    _, events = tracker.observe([agent(True, RECENT)], NOW + timedelta(seconds=5))
    assert events == []
    _, events = tracker.observe([agent(True, RECENT)], NOW + timedelta(minutes=10))
    assert [(e.old, e.new) for e in events] == [(A, I)]


def test_tracker_hysteresis_when_leaving_active() -> None:
    tracker = StatusTracker(WINDOW, grace=timedelta(seconds=5))
    activity = NOW - WINDOW
    tracker.observe([agent(True, activity)], NOW)
    # 3 s past the window: still inside the grace period, no flap to idle
    updated, events = tracker.observe([agent(True, activity)], NOW + timedelta(seconds=3))
    assert updated[0].status is A and events == []
    updated, events = tracker.observe([agent(True, activity)], NOW + timedelta(seconds=6))
    assert updated[0].status is I


def test_tracker_marks_vanished_agents_stopped() -> None:
    tracker = StatusTracker(WINDOW)
    tracker.observe([agent(True, RECENT, "a1"), agent(True, OLD, "a2")], NOW)
    _, events = tracker.observe([agent(True, RECENT, "a1")], NOW)
    assert [(e.agent_id, e.old, e.new) for e in events] == [("a2", I, S)]
    _, events = tracker.observe([agent(True, RECENT, "a1")], NOW)
    assert events == []


def test_tracker_flags_clock_skew() -> None:
    tracker = StatusTracker(WINDOW)
    tracker.observe([agent(True, NOW + timedelta(hours=1))], NOW)
    assert tracker.skewed == {"a1"}
    tracker.observe([agent(True, RECENT)], NOW)
    assert tracker.skewed == set()
