"""Runtime controls for active hub task execution."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from subprocess import Popen

logger = logging.getLogger(__name__)


class TaskCancelled(RuntimeError):
    """Raised when a task run is explicitly cancelled by the operator."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ActiveTaskHandle:
    run_id: str
    cancel_requested: bool = False
    cancellation_reason: str | None = None
    selected_agent_id: str | None = None
    process: Popen[str] | None = None
    stop_reply_sent: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def attach_process(self, process: Popen[str], *, agent_id: str) -> None:
        with self._lock:
            self.process = process
            self.selected_agent_id = agent_id

    def clear_process(self) -> None:
        with self._lock:
            self.process = None

    def request_cancel(self, reason: str) -> None:
        with self._lock:
            self.cancel_requested = True
            self.cancellation_reason = reason
            process = self.process

        if process is None or process.poll() is not None:
            return

        try:
            process.terminate()
        except Exception as exc:
            logger.warning("Could not terminate active specialist subprocess: %s", exc)
            return

        try:
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception as exc:
                logger.warning("Could not kill active specialist subprocess: %s", exc)

    def mark_stop_reply_sent(self) -> None:
        with self._lock:
            self.stop_reply_sent = True


class TaskControlRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, ActiveTaskHandle] = {}
        self._lock = threading.Lock()

    def register_run(self, run_id: str) -> ActiveTaskHandle:
        handle = ActiveTaskHandle(run_id=run_id)
        with self._lock:
            self._handles[run_id] = handle
        return handle

    def get_handle(self, run_id: str | None) -> ActiveTaskHandle | None:
        if run_id is None:
            return None
        with self._lock:
            return self._handles.get(run_id)

    def attach_process(self, run_id: str, process: Popen[str], *, agent_id: str) -> None:
        handle = self.get_handle(run_id)
        if handle is not None:
            handle.attach_process(process, agent_id=agent_id)

    def clear_process(self, run_id: str) -> None:
        handle = self.get_handle(run_id)
        if handle is not None:
            handle.clear_process()

    def unregister_run(self, run_id: str) -> None:
        with self._lock:
            self._handles.pop(run_id, None)

    def request_cancel(self, run_id: str, reason: str) -> ActiveTaskHandle | None:
        handle = self.get_handle(run_id)
        if handle is None:
            return None
        handle.request_cancel(reason)
        return handle


_registry: TaskControlRegistry | None = None


def get_task_control_registry() -> TaskControlRegistry:
    global _registry
    if _registry is None:
        _registry = TaskControlRegistry()
    return _registry
