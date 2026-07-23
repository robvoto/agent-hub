"""Runtime controls for active hub task execution."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
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
            _terminate_process_tree(process, force=False)
        except Exception as exc:
            logger.warning("Could not terminate active specialist process tree: %s", exc)
            return

        try:
            process.wait(timeout=2)
        except Exception:
            try:
                _terminate_process_tree(process, force=True)
            except Exception as exc:
                logger.warning("Could not kill active specialist process tree: %s", exc)

    def mark_stop_reply_sent(self) -> None:
        with self._lock:
            self.stop_reply_sent = True


def subprocess_popen_kwargs() -> dict[str, object]:
    """Launch specialist subprocesses in their own process group/session.

    This lets Hub cancel the whole specialist tree rather than only the
    immediate parent process when a specialist shells out to child agents
    or helper commands.
    """
    if os.name == "posix":
        return {"start_new_session": True}
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if creationflags:
        return {"creationflags": creationflags}
    return {}


def _terminate_process_tree(process: Popen[str], *, force: bool) -> None:
    sig_name = "SIGKILL" if force else "SIGTERM"
    if os.name == "posix":
        pid = getattr(process, "pid", None)
        if isinstance(pid, int):
            signal_to_send = signal.SIGKILL if force else signal.SIGTERM
            try:
                os.killpg(pid, signal_to_send)
                logger.info("Sent %s to specialist process group %s", sig_name, pid)
                return
            except ProcessLookupError:
                return
            except Exception as exc:
                logger.warning(
                    "Process-group %s failed for pid %s, falling back to direct process signal: %s",
                    sig_name,
                    pid,
                    exc,
                )

    if force:
        process.kill()
    else:
        process.terminate()


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

    def list_active_run_ids(self) -> list[str]:
        """Run ids with a currently registered handle, across every session.

        A handle only exists while a specialist subprocess is actually
        dispatched (registered in HubOrchestrator.invoke, unregistered once
        that call returns) — paused runs waiting on approval/clarification
        have no live process and are intentionally excluded, since they are
        durable and resumable after a restart.
        """
        with self._lock:
            return list(self._handles.keys())

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
