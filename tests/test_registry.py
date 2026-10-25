"""Tests for the agent registry loader."""

from agent_hub import registry
from agent_hub.registry import (
    load_active_registry_report,
    load_registry,
    load_registry_report,
    spec_fingerprint,
)
from agent_hub.runtime_policy import staged_subprocess_environment, validate_runtime_config


def test_staged_subprocess_environment_only_includes_allowed_values():
    result = staged_subprocess_environment(
        {"environment_allowlist": ["SERPAPI_KEY", "SHOPPING_MAX_API_CALLS"]},
        {
            "PATH": "/usr/bin",
            "SERPAPI_KEY": "test-key",
            "SHOPPING_MAX_API_CALLS": "12",
            "HUB_BOT_TOKEN": "telegram-secret",
            "OPENAI_API_KEY": "openai-secret",
        },
    )

    assert result == {
        "PATH": "/usr/bin",
        "SERPAPI_KEY": "test-key",
        "SHOPPING_MAX_API_CALLS": "12",
    }


def test_staged_manifest_cannot_request_hub_credentials():
    import pytest

    runtime = {
        "mode": "subprocess",
        "entrypoint": "python run_agent.py",
        "working_directory": ".",
        "input_arg": "--input-json",
        "output_arg": "--output-json",
        "default_execution_mode": "execute",
        "environment_allowlist": ["SERPAPI_KEY", "HUB_BOT_TOKEN", "OPENAI_API_KEY"],
    }

    with pytest.raises(ValueError, match="Hub-only credential"):
        validate_runtime_config("unsafe-draft", runtime)


def test_test_data_folder_cannot_overlap_live_data():
    import pytest

    from agent_hub.config import _LIVE_DATA_DIR, ConfigurationError, validate_test_data_dir

    with pytest.raises(ConfigurationError, match="separate from the live Hub data folder"):
        validate_test_data_dir(_LIVE_DATA_DIR / "test")


def test_load_registry_empty_dir(tmp_path):
    """Empty registry dir returns empty list."""
    empty = tmp_path / "agents"
    empty.mkdir()
    assert load_registry(empty) == []


def test_load_registry_missing_dir(tmp_path):
    """Missing registry dir returns empty list without raising."""
    result = load_registry(tmp_path / "nonexistent")
    assert result == []


def test_load_registry_loads_agents(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    assert len(specs) == 2
    ids = {s.id for s in specs}
    assert ids == {"code-reviewer", "job-hunter"}


def test_load_registry_parses_fields(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    reviewer = next(s for s in specs if s.id == "code-reviewer")
    assert reviewer.name == "Code Reviewer"
    assert reviewer.purpose == "Reviews code for quality"
    assert reviewer.tools == ["read_file"]
    assert reviewer.version == "1.0.0"
    assert reviewer.runtime["mode"] == "subprocess"
    assert reviewer.project_context_contract == {}
    assert reviewer.task_contract == {}
    assert reviewer.target_project_access == {}
    assert reviewer.hub_integration == {
        "protocol": "subprocess",
        "supports_clarification": True,
    }
    assert reviewer.extensions == {"aliases": ["reviewer"]}


def test_load_registry_defaults_missing_hub_integration(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    job_hunter = next(s for s in specs if s.id == "job-hunter")
    assert job_hunter.hub_integration == {}


def test_load_registry_captures_unknown_fields_as_extensions(tmp_path):
    """Any non-core agent.json field is preserved in `extensions` unchanged."""
    registry = tmp_path / "agents"
    (registry / "widget-forge").mkdir(parents=True)
    (registry / "widget-forge" / "agent.json").write_text(
        '{"id": "widget-forge", "name": "Widget Forge", "purpose": "Forges widgets",'
        ' "tools": [],'
        ' "runtime": {"mode": "subprocess", "entrypoint": "fake-forge",'
        ' "working_directory": "/tmp", "input_arg": "--input-json",'
        ' "output_arg": "--output-json", "default_execution_mode": "instruction_only"},'
        ' "project_context_contract": {"supported_schema_versions": [1]},'
        ' "task_contract": {"task_kinds": ["coding_task"]},'
        ' "webhook_url": "https://example.invalid/hook",'
        ' "backlog_sheet_id": "some-sheet-id"}',
        encoding="utf-8",
    )
    specs = load_registry(registry)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.project_context_contract == {"supported_schema_versions": [1]}
    assert spec.task_contract == {"task_kinds": ["coding_task"]}
    assert spec.extensions == {
        "webhook_url": "https://example.invalid/hook",
        "backlog_sheet_id": "some-sheet-id",
    }


def test_load_registry_skips_missing_json(tmp_path):
    registry = tmp_path / "agents"
    (registry / "no-spec").mkdir(parents=True)
    # No agent.json — should be skipped silently
    specs = load_registry(registry)
    assert specs == []


def test_load_registry_skips_invalid_json(tmp_path):
    registry = tmp_path / "agents"
    (registry / "bad-agent").mkdir(parents=True)
    (registry / "bad-agent" / "agent.json").write_text("not-json", encoding="utf-8")
    specs = load_registry(registry)
    assert specs == []


def test_load_registry_report_loads_agents(sample_registry_dir):
    result = load_registry_report(sample_registry_dir)
    assert {s.id for s in result.specs} == {"code-reviewer", "job-hunter"}
    assert result.errors == []


def test_load_registry_report_surfaces_invalid_manifest(tmp_path):
    """Unlike `load_registry`, an invalid agent.json is reported, not just
    dropped, so /agents-refresh can show operators why an agent is missing
    (AGENT-HUB-040)."""
    registry = tmp_path / "agents"
    (registry / "bad-agent").mkdir(parents=True)
    (registry / "bad-agent" / "agent.json").write_text("not-json", encoding="utf-8")

    result = load_registry_report(registry)
    assert result.specs == []
    assert len(result.errors) == 1
    assert "bad-agent" in result.errors[0].source
    assert result.errors[0].message


def test_load_registry_report_missing_dir_returns_no_errors(tmp_path):
    result = load_registry_report(tmp_path / "nonexistent")
    assert result.specs == []
    assert result.errors == []


def _write_test_agent(path, *, agent_id="test-agent", runtime_mode="subprocess"):
    import json

    path.mkdir(parents=True)
    (path / "agent.json").write_text(json.dumps({
        "id": agent_id,
        "name": "Test Agent",
        "purpose": "Test profile fixture",
        "version": "0.1.0",
        "runtime": {
            "mode": runtime_mode,
            "entrypoint": "python run_agent.py",
            "working_directory": str(path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "instruction_only",
        },
    }), encoding="utf-8")


def test_active_registry_test_profile_loads_only_allowlisted_staging_agents(tmp_path, monkeypatch):
    staging = tmp_path / "staging" / "agents"
    _write_test_agent(staging / "test-agent")
    _write_test_agent(staging / "other-agent", agent_id="other-agent")
    monkeypatch.setattr(registry, "AGENT_STAGING_DIR", staging)
    monkeypatch.setenv("HUB_PROFILE", "test")
    monkeypatch.setenv("HUB_STAGED_AGENT_IDS", "test-agent")

    result = load_active_registry_report()

    assert [spec.id for spec in result.specs] == ["test-agent"]
    assert result.specs[0].extensions["hub_profile"] == "test"
    assert result.specs[0].extensions["hub_source"] == "staged"
    assert result.specs[0].runtime["working_directory"] == str(staging / "test-agent")
    assert result.errors == []


def test_active_registry_test_profile_rejects_manual_agent(tmp_path, monkeypatch):
    staging = tmp_path / "staging" / "agents"
    _write_test_agent(staging / "test-agent", runtime_mode="manual")
    monkeypatch.setattr(registry, "AGENT_STAGING_DIR", staging)
    monkeypatch.setenv("HUB_PROFILE", "test")
    monkeypatch.setenv("HUB_STAGED_AGENT_IDS", "test-agent")

    result = load_active_registry_report()

    assert result.specs == []
    assert len(result.errors) == 1
    assert "needs a Hub connector" in result.errors[0].message
    assert "runtime.mode" not in result.errors[0].message


def test_active_registry_test_profile_requires_allowlist(monkeypatch):
    monkeypatch.setenv("HUB_PROFILE", "test")
    monkeypatch.delenv("HUB_STAGED_AGENT_IDS", raising=False)

    result = load_active_registry_report()

    assert result.specs == []
    assert result.errors[0].source == "HUB_STAGED_AGENT_IDS"


def test_active_registry_live_profile_does_not_load_staging(tmp_path, monkeypatch):
    enabled = tmp_path / "enabled"
    enabled.mkdir()
    staging = tmp_path / "staging" / "agents"
    _write_test_agent(staging / "test-agent")
    monkeypatch.setattr(registry, "AGENT_REGISTRY_DIR", enabled)
    monkeypatch.setattr(registry, "AGENT_STAGING_DIR", staging)
    monkeypatch.setenv("HUB_PROFILE", "live")
    monkeypatch.setenv("HUB_STAGED_AGENT_IDS", "test-agent")

    result = load_active_registry_report()

    assert result.specs == []
    assert result.errors == []


def test_spec_fingerprint_is_stable_for_identical_content(sample_registry_dir):
    specs_a = load_registry(sample_registry_dir)
    specs_b = load_registry(sample_registry_dir)
    reviewer_a = next(s for s in specs_a if s.id == "code-reviewer")
    reviewer_b = next(s for s in specs_b if s.id == "code-reviewer")
    assert reviewer_a is not reviewer_b
    assert spec_fingerprint(reviewer_a) == spec_fingerprint(reviewer_b)


def test_spec_fingerprint_changes_when_manifest_content_changes(tmp_path):
    registry = tmp_path / "agents"
    (registry / "widget-forge").mkdir(parents=True)
    manifest_path = registry / "widget-forge" / "agent.json"
    base = (
        '{{"id": "widget-forge", "name": "Widget Forge", "purpose": "{purpose}",'
        ' "tools": [],'
        ' "runtime": {{"mode": "subprocess", "entrypoint": "fake-forge",'
        ' "working_directory": "/tmp", "input_arg": "--input-json",'
        ' "output_arg": "--output-json", "default_execution_mode": "instruction_only"}}}}'
    )
    manifest_path.write_text(base.format(purpose="Forges widgets"), encoding="utf-8")
    before = spec_fingerprint(load_registry(registry)[0])

    manifest_path.write_text(base.format(purpose="Forges better widgets"), encoding="utf-8")
    after = spec_fingerprint(load_registry(registry)[0])

    assert before != after
