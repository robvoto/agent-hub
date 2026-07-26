"""Tests for CLI help text and shutdown handling."""

from __future__ import annotations

from agent_hub.cli import _HELP_TEXT, _handle_cli_shutdown_interrupt
from agent_hub.task_control import get_task_control_registry
from agent_hub.task_runs import TASK_STATE_CANCELLED, get_task_run_store


def test_help_text_explains_current_thread_controls() -> None:
    assert "Reply normally to continue a clarification pause in the same thread." in _HELP_TEXT
    assert "Use /approve to continue an approval pause in the same thread." in _HELP_TEXT
    assert "/decide <option> [text] - answer a task waiting on a specialist decision" in _HELP_TEXT
    assert "Use /decide <option> [text] to continue a decision pause" in _HELP_TEXT
    assert "/new starts a fresh empty thread; it is not a fork." in _HELP_TEXT
    assert "Cancelled work from /stop or /reset is not resumable." in _HELP_TEXT
    assert "There is no /fork or generic /resume command yet." in _HELP_TEXT


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

