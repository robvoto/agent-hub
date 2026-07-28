"""Cross-process single-instance guard for long-running Hub entrypoints.

Two Telegram gateway processes started against the same bot token both poll
and both reply to every message — Telegram's getUpdates offset does not
prevent this, since each process tracks its own offset and its own
duplicate-update cache independently. This lock makes that failure mode
explicit (refuse to start) instead of silently doubling every reply.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DATA_DIR

logger = logging.getLogger(__name__)

SINGLETON_LOCKS_DIR = DATA_DIR / "runtime_locks"


class SingletonLockBusyError(RuntimeError):
    """Raised when another live process already holds this singleton lock."""


@dataclass
class SingletonLockLease:
    name: str
    lock_path: Path
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.lock_path.unlink(missing_ok=True)
        self.released = True
        logger.info("Released singleton lock %r at %s", self.name, self.lock_path)

    def __enter__(self) -> SingletonLockLease:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def acquire_singleton_lock(name: str) -> SingletonLockLease:
    """Acquire a named single-instance lock, or raise if another process holds it.

    A lock file left behind by a process that no longer exists (crash, kill -9)
    is detected via its recorded PID and silently reclaimed rather than treated
    as busy — otherwise a crashed process would permanently block every restart.
    """
    SINGLETON_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = SINGLETON_LOCKS_DIR / f"{name}.lock.json"
    metadata = {
        "name": name,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "created_at": int(time.time()),
    }

    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder = _read_lock_metadata(lock_path)
            holder_pid = holder.get("pid") if holder else None
            if isinstance(holder_pid, int) and not _pid_is_running(holder_pid):
                logger.warning(
                    "Removing stale singleton lock %r at %s (holder pid=%s no longer running)",
                    name,
                    lock_path,
                    holder_pid,
                )
                lock_path.unlink(missing_ok=True)
                continue
            raise SingletonLockBusyError(_busy_message(name, holder)) from None

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, sort_keys=True)
        logger.info("Acquired singleton lock %r at %s", name, lock_path)
        return SingletonLockLease(name=name, lock_path=lock_path)

    raise SingletonLockBusyError(_busy_message(name, None))


def _busy_message(name: str, holder: dict[str, Any] | None) -> str:
    if holder is None:
        return f"Another instance already holds the {name!r} lock."
    return (
        f"Another instance already holds the {name!r} lock: "
        f"pid={holder.get('pid')} host={holder.get('hostname')} "
        f"started_at={holder.get('created_at')}. Stop it before starting a new one."
    )


def _read_lock_metadata(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
