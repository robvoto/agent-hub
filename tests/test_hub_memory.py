"""Tests for explicit Hub memory commands and the typed memory foundation."""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.store.base import PutOp

from agent_hub.hub_context import ContextSource
from agent_hub.hub_memory import (
    _LEARNING_ANALYSIS_SYSTEM_PROMPT,
    _LEGACY_LEARNINGS_NS,
    HubMemoryManager,
    LearningAnalysis,
    format_learning_confirmation,
    format_learning_list,
    format_learnings_for_prompt,
)
from agent_hub.hub_skills import HubSkill, SkillProposalResult
from agent_hub.knowledge_store import SqliteStore


def test_learn_creates_explicit_learning_record(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    record = manager.learn("Remember to prefer Telegram for operator control.", source="cli")

    assert record.identifier.startswith("mem-")
    assert record.value == "Remember to prefer Telegram for operator control."
    assert record.source == "cli"
    assert record.type == "semantic"
    assert record.scope == "operator"
    assert record.status == "active"


def test_format_learning_confirmation_without_analysis_is_unchanged(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("Prefer tabs over spaces.", source="cli")

    assert format_learning_confirmation(record) == "Learned: Prefer tabs over spaces."


def test_learn_procedural_creates_procedural_record(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    record = manager.learn_procedural("Always check evidence first.", source="cli")

    assert record.type == "procedural"
    assert record.scope == "operator"
    assert record.status == "active"


def test_reclassify_moves_record_between_typed_namespaces_without_changing_id(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))

    record = manager.learn("Always check evidence first.", source="cli")
    updated = manager.reclassify(record.identifier, memory_type="procedural")

    assert updated is not None
    assert updated.identifier == record.identifier
    assert updated.type == "procedural"
    records = manager.list_learnings()
    assert len(records) == 1
    assert records[0].identifier == record.identifier
    assert records[0].type == "procedural"


def test_format_learning_confirmation_for_memory_only(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("Prefer tabs over spaces.", source="cli")
    analysis = LearningAnalysis(
        restated_lesson="Rob prefers tabs over spaces.",
        memory_type="semantic",
        action_kind="memory_only",
        suggestion="Nothing else to do.",
        code_change_needed=False,
    )

    result = format_learning_confirmation(record, analysis=analysis)

    assert result == "Learned: Rob prefers tabs over spaces."


def test_format_learning_confirmation_for_created_skill(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn_procedural(
        "Always check evidence before claiming it is unavailable.", source="cli"
    )
    analysis = LearningAnalysis(
        restated_lesson="Hub should check evidence before claiming it is unavailable.",
        memory_type="procedural",
        action_kind="skill",
        suggestion="unused",
        code_change_needed=False,
        skill_slug="evidence-checking",
        skill_title="Evidence checking procedure",
        skill_body="Always check available evidence before claiming it is unavailable.",
    )
    skill = HubSkill(
        identifier="skill-abc123",
        slug="evidence-checking",
        title="Evidence checking procedure",
        body="Always check available evidence before claiming it is unavailable.",
        version=1,
        status="active",
        source="cli",
        memory_id=record.identifier,
        created_at=datetime.now(timezone.utc),
    )
    skill_result = SkillProposalResult(accepted=True, skill=skill, reason="Created new skill.")

    result = format_learning_confirmation(
        record,
        analysis=analysis,
        relevant_skills=[skill],
        relevant_docs=[
            ContextSource(identifier="AGENTS.md", kind="documentation", content="", freshness="")
        ],
        skill_result=skill_result,
    )

    assert result == (
        "Learned: Hub should check evidence before claiming it is unavailable.\n"
        "Reusable skill updated: Evidence checking procedure (version 1)."
    )


def test_format_learning_confirmation_for_rejected_skill(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("Some lesson.", source="cli")
    analysis = LearningAnalysis(
        restated_lesson="Some lesson.",
        memory_type="semantic",
        action_kind="skill",
        suggestion="unused",
        code_change_needed=False,
        skill_slug="some-skill",
        skill_title="Some skill",
        skill_body="Body.",
    )
    skill_result = SkillProposalResult(
        accepted=False, skill=None, reason="Title matches already-active skill 'other-skill'."
    )

    result = format_learning_confirmation(record, analysis=analysis, skill_result=skill_result)

    assert result == (
        "Learned: Some lesson.\n"
        "The learning was saved; no reusable skill was changed."
    )


def test_format_learning_confirmation_for_proposal_only_action(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("This looks like a bug.", source="cli")
    analysis = LearningAnalysis(
        restated_lesson="This looks like a bug.",
        memory_type="semantic",
        action_kind="backlog",
        suggestion="File a backlog item about this.",
        code_change_needed=True,
    )

    result = format_learning_confirmation(record, analysis=analysis)

    assert result == "Learned: This looks like a bug."


def test_format_learning_confirmation_reports_analysis_failure_plainly(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    record = manager.learn("Prefer tabs over spaces.", source="cli")

    result = format_learning_confirmation(record, analysis_error="LLM request timed out")

    assert result == (
        "Learned: Prefer tabs over spaces.\n"
        "Note: follow-up analysis was unavailable, but the learning was saved."
    )


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


def test_learn_never_compacts_operator_records(tmp_path):
    """Operator-authored /learn records are never deleted by compaction."""
    calls = []
    manager = HubMemoryManager(
        SqliteStore(tmp_path / "knowledge.sqlite3"),
        summarizer=lambda records: calls.append(records) or "unused",
    )
    for i in range(30):
        manager.learn(f"Fact number {i}", source="cli")

    records = manager.list_learnings()
    assert len(records) == 30
    assert calls == []
    assert all(r.scope == "operator" for r in records)


def test_compacts_overflow_auto_scope_semantic_records(tmp_path):
    """Auto-scope semantic records (future auto-extraction) do compact once over threshold."""
    summarized_batches: list[list] = []

    def fake_summarizer(records):
        summarized_batches.append(records)
        return "Compacted summary of old notes."

    manager = HubMemoryManager(
        SqliteStore(tmp_path / "knowledge.sqlite3"), summarizer=fake_summarizer
    )
    for i in range(26):
        manager._store_record(
            f"Fact number {i}", source="auto-extraction", type="semantic", scope="auto"
        )
        manager._compact_if_needed()

    records = manager.list_learnings()

    # 15 kept verbatim + 1 compacted summary record.
    assert len(records) == 16
    assert len(summarized_batches) == 1
    assert len(summarized_batches[0]) == 11

    summary_records = [r for r in records if r.category == "compacted-summary"]
    assert len(summary_records) == 1
    assert summary_records[0].value == "Compacted summary of old notes."
    assert summary_records[0].source == "hub-compaction"
    assert summary_records[0].scope == "auto"
    assert len(summary_records[0].evidence) == 11

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


def test_migrates_legacy_flat_namespace_into_typed_semantic_namespace(tmp_path):
    store = SqliteStore(tmp_path / "knowledge.sqlite3")
    store.batch(
        [
            PutOp(
                namespace=_LEGACY_LEARNINGS_NS,
                key="mem-legacy01",
                value={
                    "value": "Old-format learning from before typed memory.",
                    "source": "cli",
                    "category": None,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            ),
            PutOp(
                namespace=_LEGACY_LEARNINGS_NS,
                key="mem-legacy02",
                value={
                    "value": "Old compacted summary.",
                    "source": "hub-compaction",
                    "category": "compacted-summary",
                    "created_at": "2026-01-02T00:00:00+00:00",
                },
            ),
        ]
    )

    manager = HubMemoryManager(store)
    records = {r.identifier: r for r in manager.list_learnings()}

    assert set(records) == {"mem-legacy01", "mem-legacy02"}
    assert records["mem-legacy01"].value == "Old-format learning from before typed memory."
    assert records["mem-legacy01"].type == "semantic"
    assert records["mem-legacy01"].scope == "operator"
    assert records["mem-legacy01"].status == "active"
    assert records["mem-legacy02"].scope == "auto"
    assert records["mem-legacy02"].category == "compacted-summary"

    # Migration is idempotent: constructing again does not duplicate or error.
    manager2 = HubMemoryManager(store)
    assert len(manager2.list_learnings()) == 2


def test_format_learnings_for_prompt_ranks_operator_ahead_of_newer_auto(tmp_path):
    """An older explicit /learn instruction must never be crowded out by a
    newer auto-inferred fact, and both scopes are labeled distinctly."""
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Operator fact (older).", source="cli")
    manager._store_record(
        "Auto fact (newer).", source="auto-extraction", type="semantic", scope="auto"
    )

    text = format_learnings_for_prompt(manager.list_learnings())
    operator_idx = text.index("Operator fact (older).")
    auto_idx = text.index("Auto fact (newer).")

    assert operator_idx < auto_idx
    assert "Operator-established hub learnings" in text
    assert "Auto-inferred hub learnings" in text


def test_format_learnings_for_prompt_keeps_operator_when_budget_favors_recency(tmp_path):
    """Item budget must not let a newer auto fact push out an older operator one."""
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Operator fact.", source="cli")
    manager._store_record(
        "Auto fact.", source="auto-extraction", type="semantic", scope="auto"
    )

    text = format_learnings_for_prompt(manager.list_learnings(), max_items=1)

    assert "Operator fact." in text
    assert "Auto fact." not in text


def test_format_learnings_for_prompt_excludes_non_active_status(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Active fact.", source="cli")
    manager._store_record(
        "Pending candidate fact.", source="auto-extraction", type="semantic", status="pending"
    )
    manager._store_record(
        "Disabled fact.", source="auto-extraction", type="semantic", status="disabled"
    )

    text = format_learnings_for_prompt(manager.list_learnings())

    assert "Active fact." in text
    assert "Pending candidate fact." not in text
    assert "Disabled fact." not in text


def test_format_learning_list_shows_type_and_status(tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Active fact.", source="cli")

    text = format_learning_list(manager.list_learnings())

    assert "type=semantic" in text
    assert "status=active" in text


def test_learning_analysis_prompt_requires_exact_slug_match_for_skill_updates():
    """Regression guard: reusing an existing skill's slug must require confident
    identity with the exact same procedure, never mere topical similarity — a
    weaker prompt here previously risked the model reusing a "related enough"
    skill's slug and silently overwriting a distinct procedure."""
    prompt = _LEARNING_ANALYSIS_SYSTEM_PROMPT

    assert "similarity alone is never grounds to reuse a slug" in prompt
    assert "unmistakably a correction or refinement of that exact same procedure" in prompt
    assert "not a related or adjacent one" in prompt
    assert "propose your own canonical skill_slug" in prompt
