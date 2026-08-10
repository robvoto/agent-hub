"""Tests for the universal Hub-to-specialist task envelope."""

from __future__ import annotations

from agent_hub.task_envelope import build_task_envelope


def _base_envelope(**overrides):
    kwargs = dict(
        task="Do the thing",
        request_id="req-1",
        run_id="run-1",
        source="agent-hub",
        execution_mode="instruction_only",
        progress_jsonl="/tmp/progress.jsonl",
    )
    kwargs.update(overrides)
    return build_task_envelope(**kwargs)


def test_project_context_fields_omitted_without_project_root():
    envelope = _base_envelope(
        project_id="proj-1", project_contract_version=1, project_fingerprint="fp"
    )
    assert "project_root" not in envelope
    assert "project_id" not in envelope
    assert "project_contract_version" not in envelope
    assert "project_fingerprint" not in envelope


def test_project_context_fields_included_alongside_project_root():
    envelope = _base_envelope(
        project_root="/repo",
        project_id="git@example.com:org/repo",
        project_contract_version=1,
        project_fingerprint="abc123",
    )
    assert envelope["project_root"] == "/repo"
    assert envelope["project_id"] == "git@example.com:org/repo"
    assert envelope["project_contract_version"] == 1
    assert envelope["project_fingerprint"] == "abc123"


def test_project_root_alone_omits_enrichment_fields():
    """A specialist whose manifest predates AGENT-HUB-039 still just sees
    project_root — enrichment fields are only added when Hub actually
    resolved a canonical context for it."""
    envelope = _base_envelope(project_root="/repo")
    assert envelope["project_root"] == "/repo"
    assert "project_id" not in envelope
    assert "project_contract_version" not in envelope
    assert "project_fingerprint" not in envelope


def test_governed_skills_are_included_only_when_selected():
    skills = [
        {
            "slug": "evidence-checking",
            "version": 2,
            "title": "Evidence checking",
            "content": "Check evidence first.",
        }
    ]

    assert _base_envelope(governed_skills=skills)["governed_skills"] == skills
    assert "governed_skills" not in _base_envelope(governed_skills=[])


def test_backlog_reference_is_included_only_when_resolved():
    reference = {
        "project_key": "git@example.com:org/repo",
        "spreadsheet_id": "sheet-123",
        "sheet_name": "Backlog",
        "item_id": "AGENT-HUB-999",
    }

    assert _base_envelope(backlog_reference=reference)["backlog_reference"] == reference
    assert "backlog_reference" not in _base_envelope(backlog_reference=None)


def test_advertised_task_kind_is_included_when_classified():
    assert _base_envelope(task_kind="backlog_refinement")["task_kind"] == "backlog_refinement"
    assert "task_kind" not in _base_envelope(task_kind=None)
