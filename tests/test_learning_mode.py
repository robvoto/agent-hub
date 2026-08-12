"""Tests for the opt-in, debounced ('dreaming') learning-mode registry."""

from __future__ import annotations

from agent_hub.learning_mode import LearningModeRegistry


class _FakeTimer:
    """Records scheduling instead of actually sleeping on a real thread."""

    instances: list["_FakeTimer"] = []

    def __init__(self, delay: float, fn) -> None:
        self.delay = delay
        self.fn = fn
        self.cancelled = False
        self.started = False
        _FakeTimer.instances.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.fn()


def _fake_timer_factory():
    _FakeTimer.instances = []
    return _FakeTimer


def test_disabled_by_default():
    registry = LearningModeRegistry(timer_factory=_fake_timer_factory())
    assert registry.is_enabled("session-1") is False


def test_notify_task_completed_does_nothing_when_disabled():
    registry = LearningModeRegistry(timer_factory=_fake_timer_factory())
    registry.notify_task_completed("session-1", lambda session_id: None)
    assert _FakeTimer.instances == []


def test_notify_task_completed_schedules_when_enabled():
    fired: list[str] = []
    registry = LearningModeRegistry(delay_seconds=42.0, timer_factory=_fake_timer_factory())
    registry.set_enabled("session-1", True)

    registry.notify_task_completed("session-1", fired.append)

    assert len(_FakeTimer.instances) == 1
    timer = _FakeTimer.instances[0]
    assert timer.delay == 42.0
    assert timer.started is True

    timer.fire()
    assert fired == ["session-1"]


def test_notify_task_completed_debounces_by_cancelling_previous_timer():
    registry = LearningModeRegistry(timer_factory=_fake_timer_factory())
    registry.set_enabled("session-1", True)

    registry.notify_task_completed("session-1", lambda session_id: None)
    first = _FakeTimer.instances[0]

    registry.notify_task_completed("session-1", lambda session_id: None)
    second = _FakeTimer.instances[1]

    assert first.cancelled is True
    assert second.cancelled is False


def test_disabling_cancels_pending_timer():
    registry = LearningModeRegistry(timer_factory=_fake_timer_factory())
    registry.set_enabled("session-1", True)
    registry.notify_task_completed("session-1", lambda session_id: None)
    timer = _FakeTimer.instances[0]

    registry.set_enabled("session-1", False)

    assert timer.cancelled is True
    assert registry.is_enabled("session-1") is False



def test_learning_preference_survives_new_session_ids():
    registry = LearningModeRegistry(timer_factory=_fake_timer_factory())
    registry.set_enabled("session-a", True)

    assert registry.is_enabled("session-a") is True
    assert registry.is_enabled("session-b") is True

    registry.set_enabled("session-b", False)

    assert registry.is_enabled("session-a") is False
    assert registry.is_enabled("session-c") is False
