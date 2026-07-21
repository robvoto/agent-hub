"""Tests for the task-run lifecycle store."""

import pytest

from agent_hub.task_runs import (
    TASK_STATE_CANCELLED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_RECEIVED,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TaskRunStore,
)


@pytest.fixture()
def task_store(tmp_path):
    return TaskRunStore(tmp_path / "task_runs.sqlite3")


def test_create_run_records_received_event(task_store):
    run = task_store.create_run(session_id="session-1", user_message="Do something")

    assert run.state == TASK_STATE_RECEIVED
    assert run.session_id == "session-1"
    assert run.user_message == "Do something"

    events = task_store.list_events(run.id)
    assert [event.to_state for event in events] == [TASK_STATE_RECEIVED]
    assert events[0].detail == "Task received by hub orchestrator."


def test_transition_tracks_agent_and_terminal_fields(task_store):
    run = task_store.create_run(session_id="session-1", user_message="Do something")

    task_store.transition(
        run.id,
        TASK_STATE_ROUTED,
        detail="Routed to ai-tech-lead.",
        selected_agent_id="ai-tech-lead",
    )
    task_store.transition(
        run.id,
        TASK_STATE_DISPATCHED,
        detail="Dispatched to subprocess.",
        selected_agent_id="ai-tech-lead",
        dispatched_task="Implement feature X",
    )
    updated = task_store.transition(
        run.id,
        TASK_STATE_SUCCEEDED,
        detail="Completed successfully.",
        final_response="Done",
    )

    assert updated.state == TASK_STATE_SUCCEEDED
    assert updated.selected_agent_id == "ai-tech-lead"
    assert updated.dispatched_task == "Implement feature X"
    assert updated.final_response == "Done"
    assert updated.finished_at is not None

    events = task_store.list_events(run.id)
    assert [event.to_state for event in events] == [
        TASK_STATE_RECEIVED,
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_SUCCEEDED,
    ]


def test_waiting_approval_can_resume_to_routed(task_store):
    run = task_store.create_run(session_id="session-1", user_message="Deploy prod")
    task_store.transition(run.id, TASK_STATE_ROUTED, selected_agent_id="release-agent")
    task_store.transition(run.id, TASK_STATE_DISPATCHED, selected_agent_id="release-agent")
    task_store.transition(
        run.id,
        TASK_STATE_WAITING_APPROVAL,
        detail="Need approval before deploy.",
        selected_agent_id="release-agent",
        approval_token="token-123",
    )

    paused = task_store.get_run(run.id)
    assert paused is not None
    assert paused.state == TASK_STATE_WAITING_APPROVAL
    assert paused.approval_token == "token-123"
    assert paused.finished_at is None

    resumed = task_store.transition(
        run.id,
        TASK_STATE_ROUTED,
        detail="Human approved the task.",
        selected_agent_id="release-agent",
    )
    assert resumed.state == TASK_STATE_ROUTED


def test_get_latest_active_or_paused_run_prefers_most_recent_open_run(task_store):
    older = task_store.create_run(session_id="session-1", user_message="Older task")
    task_store.transition(older.id, TASK_STATE_IN_PROGRESS)

    newer = task_store.create_run(session_id="session-1", user_message="Need approval")
    task_store.transition(newer.id, TASK_STATE_WAITING_APPROVAL)

    latest = task_store.get_latest_active_or_paused_run("session-1")

    assert latest is not None
    assert latest.id == newer.id
    assert latest.state == TASK_STATE_WAITING_APPROVAL


def test_get_latest_active_or_paused_run_returns_none_when_only_terminal_runs_exist(task_store):
    run = task_store.create_run(session_id="session-1", user_message="Done task")
    task_store.transition(run.id, TASK_STATE_SUCCEEDED)

    assert task_store.get_latest_active_or_paused_run("session-1") is None


def test_get_latest_completed_or_failed_run_returns_most_recent_terminal_run(task_store):
    succeeded = task_store.create_run(session_id="session-1", user_message="Succeeded")
    task_store.transition(succeeded.id, TASK_STATE_SUCCEEDED)

    failed = task_store.create_run(session_id="session-1", user_message="Failed")
    task_store.transition(
        failed.id,
        TASK_STATE_FAILED,
        error_message="Boom",
    )

    latest = task_store.get_latest_completed_or_failed_run("session-1")

    assert latest is not None
    assert latest.id == failed.id
    assert latest.state == TASK_STATE_FAILED


def test_cancelled_run_persists_reason_and_timestamp(task_store):
    run = task_store.create_run(session_id="session-1", user_message="Stop this")

    cancelled = task_store.transition(
        run.id,
        TASK_STATE_CANCELLED,
        cancellation_reason="Stopped by user",
    )

    assert cancelled.state == TASK_STATE_CANCELLED
    assert cancelled.cancellation_reason == "Stopped by user"
    assert cancelled.cancelled_at is not None
    assert cancelled.finished_at is not None
