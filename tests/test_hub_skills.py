"""Tests for the governed Hub runtime skill store (AGENT-HUB-044)."""

from __future__ import annotations

from langgraph.store.base import PutOp

from agent_hub.hub_skills import HubSkillStore
from agent_hub.knowledge_store import SqliteStore


def test_propose_new_skill_creates_active_version_one(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    result = store.propose_skill(
        "evidence-checking",
        "Evidence checking procedure",
        "Always check available evidence before claiming it is unavailable.",
        source="cli",
        memory_id="mem-abc123",
    )

    assert result.accepted is True
    assert result.skill is not None
    assert result.skill.slug == "evidence-checking"
    assert result.skill.version == 1
    assert result.skill.status == "active"
    assert result.skill.supersedes is None
    assert result.skill.source == "cli"
    assert result.skill.memory_id == "mem-abc123"


def test_propose_update_to_existing_slug_supersedes_old_version(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    first = store.propose_skill(
        "evidence-checking",
        "Evidence checking procedure",
        "Check evidence first.",
        source="cli",
        memory_id="mem-first",
    )

    result = store.propose_skill(
        "evidence-checking",
        "Evidence checking procedure",
        "Check evidence first, then ask for more if inconclusive.",
        source="cli",
        memory_id="mem-second",
    )

    assert result.accepted is True
    assert result.skill.version == 2
    assert result.skill.status == "active"
    assert result.skill.supersedes == first.skill.identifier
    assert result.skill.memory_id == "mem-second"

    active = store.get_active_skill("evidence-checking")
    assert active.identifier == result.skill.identifier
    assert active.version == 2

    all_skills = store._all_skills()
    old = next(s for s in all_skills if s.identifier == first.skill.identifier)
    assert old.status == "superseded"
    assert old.memory_id == "mem-first"


def test_update_switches_versions_in_one_atomic_batch(tmp_path):
    sqlite_store = SqliteStore(tmp_path / "knowledge.sqlite3")
    store = HubSkillStore(sqlite_store)
    store.propose_skill(
        "evidence-checking", "Evidence checking", "v1 body.", source="cli"
    )
    recorded_batches = []
    original_batch = sqlite_store.batch

    def recording_batch(ops):
        operations = list(ops)
        recorded_batches.append(operations)
        return original_batch(operations)

    sqlite_store.batch = recording_batch

    store.propose_skill(
        "evidence-checking", "Evidence checking", "v2 body.", source="cli"
    )

    transitions = [
        batch
        for batch in recorded_batches
        if len(batch) == 2 and all(isinstance(op, PutOp) for op in batch)
    ]
    assert len(transitions) == 1
    statuses = {op.value["status"] for op in transitions[0]}
    assert statuses == {"active", "superseded"}


def test_propose_rejects_duplicate_title_under_different_slug(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill(
        "evidence-checking", "Evidence checking procedure", "Check evidence first.", source="cli"
    )

    result = store.propose_skill(
        "check-evidence-first",
        "Evidence checking procedure",
        "A near-duplicate of the existing skill.",
        source="cli",
    )

    assert result.accepted is False
    assert result.skill is None
    assert "evidence-checking" in result.reason
    assert store.get_active_skill("check-evidence-first") is None


def test_propose_rejects_empty_or_bad_slug(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    result = store.propose_skill("", "Title", "Body text.", source="cli")

    assert result.accepted is False
    assert result.skill is None
    assert store.list_active_skills() == []


def test_propose_rejects_invalid_slug_characters(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    result = store.propose_skill("Evidence Checking!", "Title", "Body text.", source="cli")

    assert result.accepted is False
    assert store.list_active_skills() == []


def test_propose_rejects_empty_title(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    result = store.propose_skill("some-skill", "   ", "Body text.", source="cli")

    assert result.accepted is False
    assert store.list_active_skills() == []


def test_propose_rejects_empty_body(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    result = store.propose_skill("some-skill", "Title", "   ", source="cli")

    assert result.accepted is False
    assert store.list_active_skills() == []


def test_propose_rejects_oversized_title_and_body(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    oversized_title = store.propose_skill("some-skill", "T" * 201, "Body.", source="cli")
    oversized_body = store.propose_skill("some-skill", "Title", "B" * 4001, source="cli")

    assert oversized_title.accepted is False
    assert oversized_body.accepted is False
    assert store.list_active_skills() == []


def test_rollback_reactivates_previous_version(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    first = store.propose_skill("evidence-checking", "Evidence checking", "v1 body.", source="cli")
    store.propose_skill("evidence-checking", "Evidence checking", "v2 body.", source="cli")

    rolled_back = store.rollback_skill("evidence-checking")

    assert rolled_back is not None
    assert rolled_back.identifier == first.skill.identifier
    assert rolled_back.version == 1
    assert rolled_back.status == "active"

    active = store.get_active_skill("evidence-checking")
    assert active.version == 1


def test_rollback_switches_versions_in_one_atomic_batch(tmp_path):
    sqlite_store = SqliteStore(tmp_path / "knowledge.sqlite3")
    store = HubSkillStore(sqlite_store)
    store.propose_skill(
        "evidence-checking", "Evidence checking", "v1 body.", source="cli"
    )
    store.propose_skill(
        "evidence-checking", "Evidence checking", "v2 body.", source="cli"
    )
    recorded_batches = []
    original_batch = sqlite_store.batch

    def recording_batch(ops):
        operations = list(ops)
        recorded_batches.append(operations)
        return original_batch(operations)

    sqlite_store.batch = recording_batch

    store.rollback_skill("evidence-checking")

    transitions = [
        batch
        for batch in recorded_batches
        if len(batch) == 2 and all(isinstance(op, PutOp) for op in batch)
    ]
    assert len(transitions) == 1
    statuses = {op.value["status"] for op in transitions[0]}
    assert statuses == {"active", "disabled"}


def test_rollback_with_no_prior_version_returns_none(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill("evidence-checking", "Evidence checking", "v1 body.", source="cli")

    assert store.rollback_skill("evidence-checking") is None


def test_rollback_unknown_slug_returns_none(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    assert store.rollback_skill("does-not-exist") is None


def test_disable_skill_retracts_with_no_replacement(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill("evidence-checking", "Evidence checking", "v1 body.", source="cli")

    assert store.disable_skill("evidence-checking") is True
    assert store.get_active_skill("evidence-checking") is None


def test_disable_unknown_skill_returns_false(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))

    assert store.disable_skill("does-not-exist") is False


def test_list_and_get_active_skills_exclude_superseded_and_disabled(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill("evidence-checking", "Evidence checking", "v1 body.", source="cli")
    store.propose_skill("evidence-checking", "Evidence checking", "v2 body.", source="cli")
    store.propose_skill("other-skill", "Other skill", "Some other body.", source="cli")
    store.disable_skill("other-skill")

    active = store.list_active_skills()

    assert len(active) == 1
    assert active[0].slug == "evidence-checking"
    assert active[0].version == 2
    assert store.get_active_skill("other-skill") is None


def test_find_relevant_skills_matches_active_skills_by_keyword(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill(
        "evidence-checking",
        "Evidence checking procedure",
        "Always check available evidence before claiming it is unavailable.",
        source="cli",
    )
    store.propose_skill(
        "telegram-formatting",
        "Telegram formatting rules",
        "Keep Telegram progress messages short and plain text.",
        source="cli",
    )

    results = store.find_relevant_skills("how should hub check evidence")

    assert len(results) == 1
    assert results[0].slug == "evidence-checking"


def test_find_relevant_skills_respects_max_items(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    for i in range(5):
        store.propose_skill(
            f"skill-{i}", f"Skill {i}", "Widget related procedure text.", source="cli"
        )

    results = store.find_relevant_skills("widget procedure", max_items=2)

    assert len(results) == 2


def test_find_relevant_skills_excludes_disabled_and_superseded(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill(
        "evidence-checking", "Evidence checking", "Check evidence first.", source="cli"
    )
    store.propose_skill(
        "evidence-checking", "Evidence checking", "Check evidence first, updated.", source="cli"
    )
    store.propose_skill(
        "widget-forge", "Widget forge", "Forge widgets carefully.", source="cli"
    )
    store.disable_skill("widget-forge")

    assert store.find_relevant_skills("evidence") != []
    assert store.find_relevant_skills("widget forge") == []


def test_find_relevant_skills_with_no_matching_terms_returns_empty(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    store.propose_skill(
        "evidence-checking", "Evidence checking", "Check evidence first.", source="cli"
    )

    assert store.find_relevant_skills("completely unrelated query about spaceships") == []


def test_select_for_dispatch_limits_count_and_total_content(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    for i, size in enumerate((1000, 3000, 2500, 500)):
        store.propose_skill(
            f"dispatch-{i}",
            f"Dispatch procedure {i}",
            "dispatch " + ("x" * size),
            source="cli",
        )

    selected = store.select_for_dispatch("dispatch procedure")

    assert len(selected) == 3
    assert sum(len(skill.body) for skill in selected) <= 6000


def test_select_for_dispatch_skips_oversized_skill_without_truncating(tmp_path):
    store = HubSkillStore(SqliteStore(tmp_path / "knowledge.sqlite3"))
    # Stored bodies are capped at 4,000 chars, so use a deliberately smaller
    # dispatch budget to prove complete skills are skipped rather than cut.
    store.propose_skill(
        "large-dispatch",
        "Large dispatch procedure",
        "dispatch " + ("x" * 1000),
        source="cli",
    )
    store.propose_skill(
        "small-dispatch",
        "Small dispatch procedure",
        "dispatch safely",
        source="cli",
    )

    selected = store.select_for_dispatch("dispatch procedure", max_total_chars=100)

    assert [skill.slug for skill in selected] == ["small-dispatch"]
    assert selected[0].body == "dispatch safely"
