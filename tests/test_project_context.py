"""Tests for the per-session current-project registry."""

from __future__ import annotations

import pytest

from agent_hub.project_context import ProjectContextRegistry


def test_get_returns_none_when_unset():
    registry = ProjectContextRegistry()
    assert registry.get("session-1") is None


def test_set_and_get_roundtrip(tmp_path):
    registry = ProjectContextRegistry()
    resolved = registry.set("session-1", str(tmp_path))
    assert resolved == str(tmp_path.resolve())
    assert registry.get("session-1") == str(tmp_path.resolve())


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
