"""Tests for the agent registry loader."""

from agent_hub.registry import find_agent, load_registry


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
    assert reviewer.aliases == ["reviewer"]
    assert reviewer.tools == ["read_file"]
    assert reviewer.version == "1.0.0"
    assert reviewer.runtime["mode"] == "subprocess"


def test_load_registry_skips_missing_json(tmp_path):
    registry = tmp_path / "agents"
    (registry / "no-spec").mkdir(parents=True)
    # No agent.json — should be skipped silently
    specs = load_registry(registry)
    assert specs == []


def test_load_registry_skips_invalid_json(tmp_path):
    registry = tmp_path / "agents"
    (registry / "bad-agent").mkdir(parents=True)
    (registry / "bad-agent" / "agent.json").write_text("not-json")
    specs = load_registry(registry)
    assert specs == []


def test_find_agent_by_id(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    result = find_agent(specs, "code-reviewer")
    assert result is not None
    assert result.id == "code-reviewer"


def test_find_agent_by_alias(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    result = find_agent(specs, "reviewer")
    assert result is not None
    assert result.id == "code-reviewer"


def test_find_agent_case_insensitive(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    # Both ID and alias lookups are case-insensitive
    assert find_agent(specs, "Code-Reviewer") is not None
    assert find_agent(specs, "REVIEWER") is not None


def test_find_agent_not_found(sample_registry_dir):
    specs = load_registry(sample_registry_dir)
    assert find_agent(specs, "nonexistent") is None
