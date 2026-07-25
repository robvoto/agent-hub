"""Test isolation fixtures for agent-hub."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_checkpointer(tmp_path, monkeypatch):
    import agent_hub.checkpointer as cp_mod

    monkeypatch.setattr(cp_mod, "CHECKPOINT_DB", tmp_path / "checkpoints.sqlite3")
    monkeypatch.setattr(cp_mod, "_conn", None)
    monkeypatch.setattr(cp_mod, "_checkpointer", None)


@pytest.fixture(autouse=True)
def _isolate_knowledge_store(tmp_path, monkeypatch):
    import agent_hub.knowledge_store as ks_mod

    monkeypatch.setattr(ks_mod, "_store", None)
    monkeypatch.setattr(ks_mod, "KNOWLEDGE_DB", tmp_path / "knowledge_store.sqlite3")


@pytest.fixture(autouse=True)
def _isolate_task_runs(tmp_path, monkeypatch):
    import agent_hub.task_runs as tr_mod

    monkeypatch.setattr(tr_mod, "_store", None)
    monkeypatch.setattr(tr_mod, "TASK_RUN_DB", tmp_path / "task_runs.sqlite3")


@pytest.fixture(autouse=True)
def _isolate_cost_log(tmp_path, monkeypatch):
    import agent_hub.cost_log as cl_mod

    monkeypatch.setattr(cl_mod, "USAGE_LOG_FILE", tmp_path / "llm_usage.json")
    monkeypatch.setattr(cl_mod, "LLM_COST_CATALOG_FILE", tmp_path / "llm_costs.json")
    (tmp_path / "llm_costs.json").write_text('{"models": {}}', encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_manifest_cache(tmp_path, monkeypatch):
    import agent_hub.manifest_cache as mc_mod

    monkeypatch.setattr(mc_mod, "_cache", None)
    monkeypatch.setattr(mc_mod, "MANIFEST_CACHE_FILE", tmp_path / "agent_manifest_cache.json")


@pytest.fixture(autouse=True)
def _isolate_task_control_registry(monkeypatch):
    import agent_hub.task_control as tc_mod

    monkeypatch.setattr(tc_mod, "_registry", None)


@pytest.fixture(autouse=True)
def _isolate_project_context_registry(monkeypatch):
    import agent_hub.project_context as pc_mod

    monkeypatch.setattr(pc_mod, "_registry", None)


@pytest.fixture(autouse=True)
def _isolate_learning_mode_registry(monkeypatch):
    import agent_hub.learning_mode as lm_mod

    monkeypatch.setattr(lm_mod, "_registry", None)


@pytest.fixture()
def sample_registry_dir(tmp_path):
    """Create a minimal agent registry directory for testing."""
    registry = tmp_path / "agents"
    (registry / "code-reviewer").mkdir(parents=True)
    (registry / "code-reviewer" / "agent.json").write_text(
        '{"id": "code-reviewer", "name": "Code Reviewer", "purpose": "Reviews code for quality",'
        ' "aliases": ["reviewer"], "tools": ["read_file"], "version": "1.0.0",'
        ' "runtime": {"mode": "subprocess", "entrypoint": "fake-reviewer",'
        ' "working_directory": "/tmp", "input_arg": "--input-json",'
        ' "output_arg": "--output-json", "default_execution_mode": "instruction_only"},'
        ' "hub_integration": {"protocol": "subprocess", "supports_clarification": true}}',
        encoding="utf-8",
    )
    (registry / "job-hunter").mkdir()
    (registry / "job-hunter" / "agent.json").write_text(
        '{"id": "job-hunter", "name": "Job Hunter", "purpose": "Finds job listings",'
        ' "tools": [], "version": "2.0.0",'
        ' "runtime": {"mode": "subprocess", "entrypoint": "fake-hunter",'
        ' "working_directory": "/tmp", "input_arg": "--input-json",'
        ' "output_arg": "--output-json", "default_execution_mode": "instruction_only"}}',
        encoding="utf-8",
    )
    return registry
