"""Tests for the persisted current-session-id pointer (AGENT-HUB-032)."""

from __future__ import annotations

from langgraph.store.memory import InMemoryStore

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


def test_scoped_session_ids_persist_independently():
    store = InMemoryStore()

    primary = load_or_create_session_id(store)
    chat_a = load_or_create_session_id(store, scope="telegram-a")
    chat_b = load_or_create_session_id(store, scope="telegram-b")

    assert len({primary, chat_a, chat_b}) == 3

    persist_session_id("new-chat-a-session", store, scope="telegram-a")

    assert load_or_create_session_id(store, scope="telegram-a") == "new-chat-a-session"
    assert load_or_create_session_id(store, scope="telegram-b") == chat_b
    assert load_or_create_session_id(store) == primary
