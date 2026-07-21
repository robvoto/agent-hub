"""Tests for explicit Hub memory commands."""

from __future__ import annotations

from agent_hub.hub_memory import (
    HubMemoryManager,
    format_learning_list,
    format_learnings_for_prompt,
)
from agent_hub.knowledge_store import SqliteStore


def test_learn_creates_explicit_learning_record(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    record = manager.learn("Remember to prefer Telegram for operator control.", source="cli")

    assert record.identifier.startswith("mem-")
    assert record.value == "Remember to prefer Telegram for operator control."
    assert record.source == "cli"


def test_memory_lists_stored_learnings(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("First fact", source="cli")
    manager.learn("Second fact", source="telegram chat 42")

    message = format_learning_list(manager.list_learnings())

    assert "Stored hub learnings:" in message
    assert "First fact" in message
    assert "Second fact" in message


def test_forget_deletes_learning(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("Temporary fact", source="cli")

    assert manager.forget(record.identifier) is True
    assert manager.list_learnings() == []


def test_forget_invalid_identifier_returns_false(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    assert manager.forget("missing-id") is False


def test_memory_empty_state_is_human_readable(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    assert (
        format_learning_list(manager.list_learnings())
        == "No hub learnings have been stored yet."
    )


def test_format_learnings_for_prompt_empty_returns_blank():
    assert format_learnings_for_prompt([]) == ""


def test_format_learnings_for_prompt_includes_value_and_source(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Prefer subprocess dispatch for AI Tech Lead.", source="cli")

    text = format_learnings_for_prompt(manager.list_learnings())

    assert "Operator-established hub learnings" in text
    assert "Prefer subprocess dispatch for AI Tech Lead." in text
    assert "source: cli" in text


def test_format_learnings_for_prompt_caps_item_count(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    for i in range(5):
        manager.learn(f"Fact number {i}", source="cli")

    text = format_learnings_for_prompt(manager.list_learnings(), max_items=2)

    included = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(included) == 2
    assert "3 older learning(s) omitted" in text


def test_learn_compacts_overflow_learnings_via_summarizer(tmp_path):
    summarized_batches: list[list] = []

    def fake_summarizer(records):
        summarized_batches.append(records)
        return "Compacted summary of old notes."

    manager = HubMemoryManager(
        SqliteStore(tmp_path / "knowledge.sqlite3"), summarizer=fake_summarizer
    )
    for i in range(26):
        manager.learn(f"Fact number {i}", source="cli")

    records = manager.list_learnings()

    # 15 kept verbatim + 1 compacted summary record.
    assert len(records) == 16
    assert len(summarized_batches) == 1
    assert len(summarized_batches[0]) == 11

    summary_records = [r for r in records if r.category == "compacted-summary"]
    assert len(summary_records) == 1
    assert summary_records[0].value == "Compacted summary of old notes."
    assert summary_records[0].source == "hub-compaction"

    # The most recent facts must survive untouched.
    assert any(r.value == "Fact number 25" for r in records)
    assert not any(r.value == "Fact number 0" for r in records)


def test_learn_does_not_compact_below_threshold(tmp_path):
    calls = []
    manager = HubMemoryManager(
        SqliteStore(tmp_path / "knowledge.sqlite3"),
        summarizer=lambda records: calls.append(records) or "unused",
    )
    for i in range(10):
        manager.learn(f"Fact number {i}", source="cli")

    assert calls == []
    assert len(manager.list_learnings()) == 10


def test_format_learnings_for_prompt_caps_char_budget(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("A" * 30, source="cli")
    manager.learn("B" * 30, source="cli")

    text = format_learnings_for_prompt(manager.list_learnings(), max_chars=60)

    included = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(included) == 1
    assert "1 older learning(s) omitted" in text
