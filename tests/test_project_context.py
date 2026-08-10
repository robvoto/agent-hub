"""Tests for the per-session canonical project context registry."""

from __future__ import annotations

import configparser

import pytest

from agent_hub.project_context import (
    PROJECT_CONTRACT_VERSION,
    ProjectContextRegistry,
)


def test_get_returns_none_when_unset():
    registry = ProjectContextRegistry()
    assert registry.get("session-1") is None


def test_set_and_get_roundtrip(tmp_path):
    registry = ProjectContextRegistry()
    context = registry.set("session-1", str(tmp_path))
    assert context.root == str(tmp_path.resolve())
    assert context.contract_version == PROJECT_CONTRACT_VERSION
    assert context.project_id == str(tmp_path.resolve())
    assert context.metadata["vcs"] == "none"
    assert context.metadata["remote"] is None

    stored = registry.get("session-1")
    assert stored == context


def test_set_rejects_nonexistent_path(tmp_path):
    registry = ProjectContextRegistry()
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="is not a directory"):
        registry.set("session-1", str(missing))


def test_clear_removes_selection(tmp_path):
    registry = ProjectContextRegistry()
    registry.set("session-1", str(tmp_path))
    registry.clear("session-1")
    assert registry.get("session-1") is None


def test_sessions_are_independent(tmp_path):
    registry = ProjectContextRegistry()
    registry.set("session-1", str(tmp_path))
    assert registry.get("session-2") is None


def _write_git_remote(root, url: str) -> None:
    git_dir = root / ".git"
    git_dir.mkdir()
    config = configparser.ConfigParser()
    config.add_section('remote "origin"')
    config.set('remote "origin"', "url", url)
    with (git_dir / "config").open("w", encoding="utf-8") as fh:
        config.write(fh)


def test_project_id_derived_from_git_remote_when_present(tmp_path):
    _write_git_remote(tmp_path, "git@example.com:org/repo.git")
    registry = ProjectContextRegistry()
    context = registry.set("session-1", str(tmp_path))
    assert context.project_id == "git@example.com:org/repo"
    assert context.metadata["vcs"] == "git"
    assert context.metadata["remote"] == "git@example.com:org/repo.git"


def test_project_id_falls_back_to_path_without_remote(tmp_path):
    (tmp_path / ".git").mkdir()
    registry = ProjectContextRegistry()
    context = registry.set("session-1", str(tmp_path))
    assert context.project_id == str(tmp_path.resolve())
    assert context.metadata["vcs"] == "git"
    assert context.metadata["remote"] is None


def test_resolve_for_dispatch_returns_none_when_nothing_selected():
    registry = ProjectContextRegistry()
    resolution = registry.resolve_for_dispatch("session-1")
    assert resolution.context is None
    assert resolution.error is None


def test_resolve_for_dispatch_passes_through_a_valid_selection(tmp_path):
    registry = ProjectContextRegistry()
    context = registry.set("session-1", str(tmp_path))
    resolution = registry.resolve_for_dispatch("session-1")
    assert resolution.error is None
    assert resolution.context == context


def test_resolve_for_dispatch_stops_when_root_no_longer_exists(tmp_path):
    # The knowledge store db itself lives under `tmp_path` (see conftest's
    # `_isolate_knowledge_store`), so the removed project must be a
    # subdirectory rather than `tmp_path` itself.
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    registry = ProjectContextRegistry()
    registry.set("session-1", str(project_dir))
    import shutil

    shutil.rmtree(project_dir)

    resolution = registry.resolve_for_dispatch("session-1")
    assert resolution.context is None
    assert resolution.error is not None
    assert "no longer exists" in resolution.error


def test_resolve_for_dispatch_stops_when_identity_changed_underneath(tmp_path):
    project_a = tmp_path / "a"
    project_a.mkdir()
    registry = ProjectContextRegistry()
    registry.set("session-1", str(project_a))

    _write_git_remote(project_a, "git@example.com:org/different-repo.git")

    resolution = registry.resolve_for_dispatch("session-1")
    assert resolution.context is None
    assert resolution.error is not None
    assert "no longer matches" in resolution.error


def test_resolve_for_request_uses_one_known_project_alias(tmp_path):
    project_a = tmp_path / "alpha"
    project_b = tmp_path / "beta"
    project_a.mkdir()
    project_b.mkdir()
    registry = ProjectContextRegistry()
    context_a = registry.set("session-a", str(project_a))
    context_b = registry.set("session-b", str(project_b))

    resolution = registry.resolve_for_request("session-empty", "Code work for beta")

    assert resolution.error is None
    assert resolution.context == context_b
    assert resolution.context != context_a


def test_resolve_for_request_falls_back_to_current_project_without_project_reference(tmp_path):
    project = tmp_path / "alpha"
    project.mkdir()
    registry = ProjectContextRegistry()
    context = registry.set("session-1", str(project))

    resolution = registry.resolve_for_request("session-1", "Code AGENT-HUB-123")

    assert resolution.error is None
    assert resolution.context == context


def test_resolve_for_request_honors_explicit_known_project_over_current(tmp_path):
    project_a = tmp_path / "alpha"
    project_b = tmp_path / "beta"
    project_a.mkdir()
    project_b.mkdir()
    registry = ProjectContextRegistry()
    registry.set("session-a", str(project_a))
    context_b = registry.set("session-b", str(project_b))
    registry.set("operator", str(project_a))

    resolution = registry.resolve_for_request("operator", "project:beta Code AGENT-HUB-123")

    assert resolution.error is None
    assert resolution.context == context_b


def test_resolve_for_request_stops_on_unknown_explicit_project(tmp_path):
    project = tmp_path / "alpha"
    project.mkdir()
    registry = ProjectContextRegistry()
    registry.set("session-1", str(project))

    resolution = registry.resolve_for_request("session-1", "project:unknown Code AGENT-HUB-123")

    assert resolution.context is None
    assert resolution.error is not None
    assert "unknown project" in resolution.error


def test_resolve_for_request_stops_on_ambiguous_known_alias(tmp_path):
    project_a = tmp_path / "a" / "shared"
    project_b = tmp_path / "b" / "shared"
    project_a.mkdir(parents=True)
    project_b.mkdir(parents=True)
    registry = ProjectContextRegistry()
    registry.set("session-a", str(project_a))
    registry.set("session-b", str(project_b))

    resolution = registry.resolve_for_request("session-empty", "Code shared")

    assert resolution.context is None
    assert resolution.error is not None
    assert "multiple known projects" in resolution.error


def test_ticket_like_identifier_does_not_resolve_by_project_name_prefix(tmp_path):
    project = tmp_path / "AGENT-HUB"
    project.mkdir()
    registry = ProjectContextRegistry()
    registry.set("session-known", str(project))

    resolution = registry.resolve_for_request("session-empty", "Code AGENT-HUB-123")

    assert resolution.context is None
    assert resolution.error is None
