"""Tests for the agent registry loader."""

from agent_hub.registry import load_registry, load_registry_report, spec_fingerprint


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
        ' "webhook_url": "https://example.invalid/hook",'
        ' "backlog_sheet_id": "some-sheet-id"}',
        encoding="utf-8",
    )
    specs = load_registry(registry)
    assert len(specs) == 1
    spec = specs[0]
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
