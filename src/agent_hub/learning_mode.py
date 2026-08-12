"""Opt-in, debounced ("dreaming") automatic semantic memory extraction.

Stored as an operator-level preference so /new, /reset, and process restarts do
not silently change it. Automatic memory remains user-controlled. When the
operator turns it on, extraction is deferred until the session goes quiet, so a burst
of messages costs one extraction pass instead of one per task.

The on/off flag is persisted in the Hub knowledge store, keyed by session_id,
so a hub restart does not silently revert /learn-mode now that session_id
itself survives a restart (see AGENT-HUB-032). Pending debounce timers remain
in-memory only — a restart legitimately drops a scheduled dream pass, since
OS-level timers cannot be persisted.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from langgraph.store.base import GetOp, PutOp

from .knowledge_store import get_knowledge_store

logger = logging.getLogger(__name__)

DREAM_DELAY_SECONDS = 300.0
_NAMESPACE = ("hub", "learning_mode")
_OPERATOR_KEY = "operator-default"


class LearningModeRegistry:
    def __init__(
        self,
        *,
        delay_seconds: float = DREAM_DELAY_SECONDS,
        timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = threading.Timer,
        store: Any = None,
    ) -> None:
        self._delay_seconds = delay_seconds
        self._timer_factory = timer_factory
        self._store = store or get_knowledge_store()
        self._lock = threading.Lock()
        self._timers: dict[str, threading.Timer] = {}

    def is_enabled(self, session_id: str) -> bool:
        with self._lock:
            return self._is_enabled_locked(session_id)

    def _is_enabled_locked(self, session_id: str) -> bool:
        operator_item = self._store.batch([GetOp(namespace=_NAMESPACE, key=_OPERATOR_KEY)])[0]
        if operator_item is not None:
            return bool(operator_item.value.get("enabled"))
        # Backward-compatible migration path: honour the legacy per-session flag
        # until the operator explicitly changes the preference once.
        item = self._store.batch([GetOp(namespace=_NAMESPACE, key=session_id)])[0]
        return bool(item.value.get("enabled")) if item is not None else False

    def set_enabled(self, session_id: str, enabled: bool) -> None:
        with self._lock:
            self._store.batch(
                [PutOp(namespace=_NAMESPACE, key=_OPERATOR_KEY, value={"enabled": enabled})]
            )
            if not enabled:
                for active_session_id in tuple(self._timers):
                    self._cancel_locked(active_session_id)

    def notify_task_completed(self, session_id: str, on_fire: Callable[[str], None]) -> None:
        """Reschedule the dream timer for this session, if learning mode is on.

        Debounced: a new completed task within the delay window cancels the
        previous timer and starts a fresh one, so a burst of activity only
        triggers one extraction pass after things go quiet.
        """
        with self._lock:
            if not self._is_enabled_locked(session_id):
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
