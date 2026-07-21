"""Tests for human-readable task run status summaries."""

from __future__ import annotations

from agent_hub.run_status import format_current_run_status, format_last_run_status
from agent_hub.task_runs import (
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_WAITING_APPROVAL,
    TaskRunStore,
)


def test_format_current_run_status_for_active_run(tmp_path):
    store = TaskRunStore(tmp_path / "task_runs.sqlite3")
    run = store.create_run(session_id="session-1", user_message="Build the feature")
    store.transition(
        run.id,
        TASK_STATE_IN_PROGRESS,
        selected_agent_id="ai-tech-lead",
        dispatched_task="Implement the feature end to end",
    )
    updated = store.update_run(
        run.id,
        usage={"totals": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}},
        cost={"status": "unknown", "known_usd": None, "unknown_models": ["gpt-4.1-mini"]},
    )

    message = format_current_run_status(updated)

    assert "Current task:" in message
    assert "State: in_progress" in message
    assert "Selected agent: ai-tech-lead" in message
    assert "Task summary: Implement the feature end to end" in message
    assert "Token usage: total=18, input=11, output=7" in message
    assert "Estimated cost: Unknown" in message


def test_format_current_run_status_for_paused_run(tmp_path):
    store = TaskRunStore(tmp_path / "task_runs.sqlite3")
    run = store.create_run(session_id="session-1", user_message="Deploy to prod")
    paused = store.transition(
        run.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id="release-agent",
        dispatched_task="Deploy service to production",
        final_response="Waiting for approval",
    )

    message = format_current_run_status(paused)

    assert "State: waiting_approval" in message
    assert "Selected agent: release-agent" in message
    assert "Result or error: Waiting for approval" in message


def test_format_last_run_status_for_completed_run(tmp_path):
    store = TaskRunStore(tmp_path / "task_runs.sqlite3")
    run = store.create_run(session_id="session-1", user_message="Write docs")
    completed = store.transition(
        run.id,
        "succeeded",
        selected_agent_id="doc-agent",
        final_response="Docs updated successfully.",
        duration_ms=1250,
        usage={"totals": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}},
        cost={"status": "estimated", "known_usd": 0.012345, "unknown_models": []},
    )

    message = format_last_run_status(completed)

    assert "Last task:" in message
    assert "State: succeeded" in message
    assert "Duration: 1s" in message
    assert "Result or error: Docs updated successfully." in message
    assert "Estimated cost: $0.012345 (estimated)" in message


def test_format_last_run_status_for_failed_run(tmp_path):
    store = TaskRunStore(tmp_path / "task_runs.sqlite3")
    run = store.create_run(session_id="session-1", user_message="Do risky thing")
    failed = store.transition(
        run.id,
        TASK_STATE_FAILED,
        selected_agent_id="ops-agent",
        error_message="Command failed",
    )

    message = format_last_run_status(failed)

    assert "State: failed" in message
    assert "Result or error: Command failed" in message


def test_empty_status_messages_are_human_readable():
    assert format_current_run_status(None) == "No task is currently active or paused."
    assert format_last_run_status(None) == "No completed or failed task has been recorded yet."
