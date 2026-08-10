"""Tests for persistent typed resources owned by canonical projects."""

from __future__ import annotations

import pytest

from agent_hub.project_context import ProjectContextRegistry
from agent_hub.project_resources import (
    BACKLOG_RESOURCE_TYPE,
    ProjectResourceRegistry,
    build_backlog_reference,
)


def _project(tmp_path, session_id: str = "session-1"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return ProjectContextRegistry().set(session_id, str(tmp_path))


def test_register_backlog_uses_canonical_project_identity(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()

    resource = registry.register_backlog(
        context,
        location={"provider": "google_sheets", "spreadsheet_id": "sheet-123", "gid": 42},
        source="README.md",
        metadata={"title": "Agent Hub backlog", "refresh": "manual"},
    )

    assert resource.project_id == context.project_id
    assert resource.resource_type == BACKLOG_RESOURCE_TYPE
    assert resource.location == {
        "provider": "google_sheets",
        "spreadsheet_id": "sheet-123",
        "gid": 42,
    }
    assert resource.source == "README.md"
    assert resource.metadata == {"title": "Agent Hub backlog", "refresh": "manual"}
    assert resource.provenance == ("README.md",)
    assert resource.reinforcement_count == 0
    assert resource.created_at.tzinfo is not None
    assert resource.updated_at.tzinfo is not None
    assert registry.list(context, resource_type=BACKLOG_RESOURCE_TYPE) == [resource]


def test_same_project_type_and_location_updates_in_place(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()
    location = {"provider": "google_sheets", "spreadsheet_id": "sheet-123"}

    first = registry.register_backlog(
        context, location=location, source="README.md", metadata={"title": "old"}
    )
    second = registry.register_backlog(
        context,
        location={"spreadsheet_id": "sheet-123", "provider": "google_sheets"},
        source="operator",
        metadata={"title": "current"},
    )

    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert second.source == "operator"
    assert second.metadata == {"title": "current"}
    assert second.provenance == ("README.md", "operator")
    assert second.reinforcement_count == 1
    assert registry.list(context) == [second]


def test_registry_supports_future_resource_types_and_project_scoping(tmp_path):
    project_a = _project(tmp_path / "a", "session-a")
    project_b = _project(tmp_path / "b", "session-b")
    registry = ProjectResourceRegistry()

    resource = registry.register(
        project_a,
        resource_type="documentation",
        location={"uri": "https://example.test/docs"},
        source="operator",
        metadata={"format": "html"},
    )

    assert registry.get(
        project_a,
        resource_type="documentation",
        location={"uri": "https://example.test/docs"},
    ) == resource
    assert registry.list(project_b) == []


def test_registry_persists_across_registry_instances(tmp_path):
    context = _project(tmp_path)
    first_registry = ProjectResourceRegistry()
    first_registry.register_backlog(
        context,
        location={"spreadsheet_id": "sheet-123"},
        source="README.md",
    )

    second_registry = ProjectResourceRegistry()
    resources = second_registry.list(context)

    assert len(resources) == 1
    assert resources[0].location == {"spreadsheet_id": "sheet-123"}


def test_registry_rejects_unstructured_or_missing_identity(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()

    with pytest.raises(ValueError, match="location cannot be empty"):
        registry.register_backlog(context, location={}, source="README.md")
    with pytest.raises(ValueError, match="source cannot be empty"):
        registry.register_backlog(context, location={"id": "sheet-123"}, source=" ")


def test_resolve_and_build_backlog_reference_fail_closed_on_ambiguity(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()
    first = registry.register_backlog(
        context,
        location={
            "spreadsheet_id": "sheet-123",
            "sheet_name": "Backlog",
        },
        source="memory:one",
    )
    second = registry.register_backlog(
        context,
        location={
            "spreadsheet_id": "sheet-456",
            "sheet_name": "Backlog",
        },
        source="memory:two",
    )

    assert build_backlog_reference(first, item_id="AGENT-HUB-1") == {
        "project_key": context.project_id,
        "spreadsheet_id": "sheet-123",
        "sheet_name": "Backlog",
        "item_id": "AGENT-HUB-1",
    }
    resolution = registry.resolve(context, resource_type=BACKLOG_RESOURCE_TYPE)
    assert resolution.resource is None
    assert resolution.error and "multiple" in resolution.error
    assert second != first


def test_request_resolution_pairs_one_source_with_exact_request_item(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()
    resource = registry.register_backlog(
        context,
        location={
            "spreadsheet_id": "sheet-123",
            "sheet_name": "Backlog",
        },
        source="operator",
    )

    resolved = registry.resolve_for_request(
        context,
        resource_type=BACKLOG_RESOURCE_TYPE,
        request_text="Please work on ITEM-42.",
    )
    assert resolved.resource == resource
    assert resolved.item_id == "ITEM-42"
    other_item = registry.resolve_for_request(
        context,
        resource_type=BACKLOG_RESOURCE_TYPE,
        request_text="Please work on ITEM-420.",
    )
    assert other_item.resource == resource
    assert other_item.item_id == "ITEM-420"


def test_backlog_item_identity_is_not_persisted_or_used_as_resource_identity(tmp_path):
    context = _project(tmp_path)
    registry = ProjectResourceRegistry()
    first = registry.register_backlog(
        context,
        location={
            "provider": "google_sheets",
            "spreadsheet_id": "sheet-123",
            "sheet_name": "Backlog",
            "item_id": "ITEM-1",
        },
        source="operator",
    )
    second = registry.register_backlog(
        context,
        location={
            "provider": "google_sheets",
            "spreadsheet_id": "sheet-123",
            "sheet_name": "Backlog",
            "item_id": "ITEM-2",
        },
        source="memory",
    )

    assert first.location == second.location == {
        "provider": "google_sheets",
        "spreadsheet_id": "sheet-123",
        "sheet_name": "Backlog",
    }
    assert "item_id" not in second.location
    assert registry.list(context, resource_type=BACKLOG_RESOURCE_TYPE) == [second]
