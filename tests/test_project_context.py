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
