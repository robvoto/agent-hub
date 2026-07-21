"""Opt-in, debounced ("dreaming") automatic semantic memory extraction.

Off by default per session — matching how ChatGPT, Mem0, and Letta all ship
automatic memory as user-controlled, never silently always-on. When a Rob
turns it on, extraction is deferred until the session goes quiet, so a burst
of messages costs one extraction pass instead of one per task.

In-memory only: a hub restart loses both the on/off flag and any pending
timer. That is an accepted tradeoff, not a bug — see HUB-LEARN-002.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

DREAM_DELAY_SECONDS = 300.0


class LearningModeRegistry:
    def __init__(
        self,
        *,
        delay_seconds: float = DREAM_DELAY_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = threading.Timer,
    ) -> None:
        self._delay_seconds = delay_seconds
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._enabled: dict[str, bool] = {}
        self._timers: dict[str, threading.Timer] = {}

    def is_enabled(self, session_id: str) -> bool:
        with self._lock:
            return self._enabled.get(session_id, False)

    def set_enabled(self, session_id: str, enabled: bool) -> None:
        with self._lock:
            self._enabled[session_id] = enabled
            if not enabled:
                self._cancel_locked(session_id)

    def notify_task_completed(self, session_id: str, on_fire: Callable[[str], None]) -> None:
        """Reschedule the dream timer for this session, if learning mode is on.

        Debounced: a new completed task within the delay window cancels the
        previous timer and starts a fresh one, so a burst of activity only
        triggers one extraction pass after things go quiet.
        """
        with self._lock:
            if not self._enabled.get(session_id, False):
                return
            self._cancel_locked(session_id)
            timer = self._timer_factory(self._delay_seconds, lambda: on_fire(session_id))
            timer.daemon = True
            self._timers[session_id] = timer
            timer.start()
            logger.debug(
                "Learning mode: scheduled dream pass for session %s in %.0fs",
                session_id,
                self._delay_seconds,
            )

    def cancel(self, session_id: str) -> None:
        with self._lock:
            self._cancel_locked(session_id)

    def _cancel_locked(self, session_id: str) -> None:
        existing = self._timers.pop(session_id, None)
        if existing is not None:
            existing.cancel()


_registry: LearningModeRegistry | None = None


def get_learning_mode_registry() -> LearningModeRegistry:
    global _registry
    if _registry is None:
        _registry = LearningModeRegistry()
    return _registry
