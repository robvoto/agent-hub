"""Tests for the persisted current-session-id pointer (AGENT-HUB-032)."""

from __future__ import annotations

from agent_hub.session_state import load_or_create_session_id, persist_session_id


def test_first_ever_run_creates_and_persists_a_session_id():
    session_id = load_or_create_session_id()

    assert session_id
    assert load_or_create_session_id() == session_id


def test_restart_resumes_the_existing_session_id():
    first_start = load_or_create_session_id()

    second_start = load_or_create_session_id()

    assert second_start == first_start


def test_persist_session_id_rolls_the_pointer_forward_for_the_next_start():
    load_or_create_session_id()

    persist_session_id("new-session-after-reset")

    assert load_or_create_session_id() == "new-session-after-reset"
