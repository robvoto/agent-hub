"""Tests for CLI help text and shutdown handling."""

from __future__ import annotations

from agent_hub.cli import _HELP_TEXT, _handle_cli_shutdown_interrupt
from agent_hub.task_control import get_task_control_registry
from agent_hub.task_runs import TASK_STATE_CANCELLED, get_task_run_store


def test_help_text_explains_task_controls() -> None:
    assert "/agents-refresh - refresh specialist registry" in _HELP_TEXT
    assert "/agents-status - show specialist registry health" in _HELP_TEXT
    assert "/tasks - list all active/paused tasks" in _HELP_TEXT
    assert "/resume <id> - select a paused task to continue" in _HELP_TEXT
    assert "/status - show details for this conversation's current task" in _HELP_TEXT
    assert "/stop [id] - cancel the current task, or a specific task" in _HELP_TEXT
    assert "/reset-all - cancel all active/paused tasks and start fresh" in _HELP_TEXT
    assert "Thread model:" not in _HELP_TEXT
    assert "There is no /fork" not in _HELP_TEXT
    assert "/hub-status - show Hub status" in _HELP_TEXT


def test_handle_cli_shutdown_interrupt_cancels_active_tasks_and_reports_count(capsys):
    run = get_task_run_store().create_run(session_id="s1", user_message="hi")
    get_task_control_registry().register_run(run.id)

    _handle_cli_shutdown_interrupt()

    captured = capsys.readouterr()
    assert "Cancelled 1 in-flight task(s)." in captured.out
    assert "Bye." in captured.out
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_CANCELLED


def test_handle_cli_shutdown_interrupt_is_quiet_when_nothing_active(capsys):
    _handle_cli_shutdown_interrupt()

    captured = capsys.readouterr()
    assert "Cancelled" not in captured.out
    assert "Bye." in captured.out

