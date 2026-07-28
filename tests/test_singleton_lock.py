from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent_hub.singleton_lock as singleton_lock
from agent_hub.singleton_lock import SingletonLockBusyError, acquire_singleton_lock


@pytest.fixture(autouse=True)
def _isolated_locks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(singleton_lock, "SINGLETON_LOCKS_DIR", tmp_path / "runtime_locks")


def test_second_acquire_is_rejected_while_first_is_held() -> None:
    first = acquire_singleton_lock("telegram-gateway")

    try:
        with pytest.raises(SingletonLockBusyError, match="telegram-gateway"):
            acquire_singleton_lock("telegram-gateway")
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release() -> None:
    first = acquire_singleton_lock("telegram-gateway")
    first.release()

    second = acquire_singleton_lock("telegram-gateway")
    try:
        assert second.lock_path.exists()
    finally:
        second.release()


def test_different_names_do_not_conflict() -> None:
    first = acquire_singleton_lock("telegram-gateway")
    second = acquire_singleton_lock("some-other-service")

    try:
        assert first.lock_path != second.lock_path
    finally:
        first.release()
        second.release()


def test_stale_lock_from_a_dead_process_is_reclaimed() -> None:
    singleton_lock.SINGLETON_LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    stale_path = singleton_lock.SINGLETON_LOCKS_DIR / "telegram-gateway.lock.json"
    stale_path.write_text(
        json.dumps(
            {
                "name": "telegram-gateway",
                "pid": 999999,
                "hostname": "stale-host",
                "created_at": 0,
            }
        ),
        encoding="utf-8",
    )

    lease = acquire_singleton_lock("telegram-gateway")

    try:
        payload = json.loads(lease.lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    finally:
        lease.release()


def test_busy_error_names_the_holder_pid_and_host() -> None:
    first = acquire_singleton_lock("telegram-gateway")

    try:
        with pytest.raises(SingletonLockBusyError, match=str(os.getpid())):
            acquire_singleton_lock("telegram-gateway")
    finally:
        first.release()


def test_release_is_idempotent() -> None:
    lease = acquire_singleton_lock("telegram-gateway")
    lease.release()
    lease.release()  # must not raise


def test_context_manager_releases_on_exit() -> None:
    with acquire_singleton_lock("telegram-gateway") as lease:
        assert lease.lock_path.exists()
    assert not lease.lock_path.exists()
