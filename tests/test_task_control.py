"""Tests for the active-task-handle registry."""

from __future__ import annotations

from agent_hub.task_control import get_task_control_registry


def test_list_active_run_ids_empty_when_nothing_registered():
    assert get_task_control_registry().list_active_run_ids() == []


def test_list_active_run_ids_includes_registered_runs():
    registry = get_task_control_registry()
    registry.register_run("run-1")
    registry.register_run("run-2")

    assert sorted(registry.list_active_run_ids()) == ["run-1", "run-2"]


def test_list_active_run_ids_excludes_unregistered_runs():
    registry = get_task_control_registry()
    registry.register_run("run-1")
    registry.register_run("run-2")
    registry.unregister_run("run-1")

    assert registry.list_active_run_ids() == ["run-2"]
