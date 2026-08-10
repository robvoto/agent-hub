"""Persistent resources owned by a canonical Hub project.

Project identity remains owned by :mod:`project_context`.  This registry only
associates resource records with a ``ProjectContext.project_id``; it never
derives, assigns, or persists a second project identity.

Resources are keyed by project, resource type, and structured location.  A
repeat registration of that same resource updates the existing record instead
of creating a duplicate.  The registry accepts arbitrary non-empty resource
types so future types can use the generic API without changing this module;
``backlog`` is the first named convenience type.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langgraph.store.base import GetOp, PutOp, SearchOp

from .knowledge_store import SqliteStore, get_knowledge_store
from .project_context import ProjectContext

_NAMESPACE = ("hub", "project_resources")
BACKLOG_RESOURCE_TYPE = "backlog"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json_mapping(value: Mapping[str, Any] | None, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping.")
    try:
        # The store is JSON-backed. Round-tripping also prevents a caller from
        # mutating a resource after it has been registered.
        copied = json.loads(json.dumps(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must contain JSON-serializable values.") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{field_name} must be a mapping.")
    return copied


def _resource_key(project_id: str, resource_type: str, location: Mapping[str, Any]) -> str:
    location_json = json.dumps(location, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(location_json.encode("utf-8")).hexdigest()[:16]
    return f"{project_id}|{resource_type}|{digest}"


@dataclass(frozen=True)
class ProjectResource:
    """A typed resource associated with one canonical project identity."""

    project_id: str
    resource_type: str
    location: dict[str, Any]
    source: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    provenance: tuple[str, ...] = ()
    reinforcement_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "resource_type": self.resource_type,
            "location": copy.deepcopy(self.location),
            "source": self.source,
            "metadata": copy.deepcopy(self.metadata),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "provenance": list(self.provenance),
            "reinforcement_count": self.reinforcement_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ProjectResource":
        return cls(
            project_id=str(data["project_id"]),
            resource_type=str(data["resource_type"]),
            location=_json_mapping(data.get("location"), field_name="location"),
            source=str(data["source"]),
            metadata=_json_mapping(data.get("metadata"), field_name="metadata"),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            updated_at=datetime.fromisoformat(str(data["updated_at"])),
            provenance=tuple(str(value) for value in (data.get("provenance") or [])),
            reinforcement_count=int(data.get("reinforcement_count", 0)),
        )


@dataclass(frozen=True)
class ProjectResourceResolution:
    """Bounded resolution result for one resource type on a project."""

    resource: ProjectResource | None
    error: str | None
    item_id: str | None = None


def _item_to_resource(item: Any) -> ProjectResource:
    return ProjectResource.from_dict(item.value or {})


class ProjectResourceRegistry:
    """Persist typed resources against an existing canonical project context."""

    def __init__(self, store: SqliteStore | None = None) -> None:
        self._store = store or get_knowledge_store()
        self._lock = threading.Lock()

    def register(
        self,
        context: ProjectContext,
        *,
        resource_type: str,
        location: Mapping[str, Any],
        source: str,
        metadata: Mapping[str, Any] | None = None,
        provenance: Iterable[str] | None = None,
    ) -> ProjectResource:
        """Create or update a resource for ``context``.

        The canonical context is deliberately required instead of a path or a
        separately managed project object.  Exact repeats of the same
        project/type/location are upserts and retain their original creation
        timestamp.
        """
        if not isinstance(context, ProjectContext):
            raise TypeError("context must be a canonical ProjectContext.")
        project_id = context.project_id.strip()
        if not project_id:
            raise ValueError("ProjectContext.project_id cannot be empty.")

        resource_type = resource_type.strip()
        if not resource_type:
            raise ValueError("resource_type cannot be empty.")
        source = source.strip()
        if not source:
            raise ValueError("source cannot be empty.")

        clean_location = _json_mapping(location, field_name="location")
        if resource_type == BACKLOG_RESOURCE_TYPE:
            # A backlog resource is the reusable project-level source.  A row
            # identifier belongs to the current request and must never split
            # one source into item-specific resource records.
            clean_location.pop("item_id", None)
        if not clean_location:
            raise ValueError("location cannot be empty.")
        clean_metadata = _json_mapping(metadata, field_name="metadata")
        if resource_type == BACKLOG_RESOURCE_TYPE:
            clean_metadata.pop("item_id", None)
        clean_provenance = tuple(
            dict.fromkeys(
                str(value).strip() for value in (provenance or ()) if str(value).strip()
            )
        )
        if source not in clean_provenance:
            clean_provenance = (*clean_provenance, source)
        key = _resource_key(project_id, resource_type, clean_location)

        with self._lock:
            existing_item = self._store.batch([GetOp(namespace=_NAMESPACE, key=key)])[0]
            existing = _item_to_resource(existing_item) if existing_item is not None else None
            now = _now()
            existing_provenance = existing.provenance if existing is not None else ()
            merged_provenance = tuple(dict.fromkeys((*existing_provenance, *clean_provenance)))
            resource = ProjectResource(
                project_id=project_id,
                resource_type=resource_type,
                location=clean_location,
                source=source,
                metadata=clean_metadata,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
                provenance=merged_provenance,
                reinforcement_count=(existing.reinforcement_count + 1) if existing else 0,
            )
            self._store.batch(
                [PutOp(namespace=_NAMESPACE, key=key, value=resource.to_dict())]
            )
        return resource

    def register_backlog(
        self,
        context: ProjectContext,
        *,
        location: Mapping[str, Any],
        source: str,
        metadata: Mapping[str, Any] | None = None,
        provenance: Iterable[str] | None = None,
    ) -> ProjectResource:
        """Register the first supported resource type: a project backlog."""
        return self.register(
            context,
            resource_type=BACKLOG_RESOURCE_TYPE,
            location=location,
            source=source,
            metadata=metadata,
            provenance=provenance,
        )

    def get(
        self,
        context: ProjectContext,
        *,
        resource_type: str,
        location: Mapping[str, Any],
    ) -> ProjectResource | None:
        """Get the resource identified by project, type, and location."""
        project_id, resource_type, clean_location = self._identity_parts(
            context, resource_type, location
        )
        key = _resource_key(project_id, resource_type, clean_location)
        with self._lock:
            item = self._store.batch([GetOp(namespace=_NAMESPACE, key=key)])[0]
        return _item_to_resource(item) if item is not None else None

    def resolve(
        self,
        context: ProjectContext,
        *,
        resource_type: str,
    ) -> ProjectResourceResolution:
        """Resolve exactly one resource of ``resource_type`` for ``context``."""
        resources = self.list(context, resource_type=resource_type)
        if len(resources) > 1:
            return ProjectResourceResolution(
                resource=None,
                error=(
                    f"Project '{context.project_id}' has multiple '{resource_type}' "
                    "resources; refusing to choose one implicitly."
                ),
            )
        return ProjectResourceResolution(
            resource=resources[0] if resources else None,
            error=None,
        )

    def resolve_for_request(
        self,
        context: ProjectContext,
        *,
        resource_type: str,
        request_text: str,
        references: Iterable[str] = (),
    ) -> ProjectResourceResolution:
        """Resolve a source and, for backlog, an explicit request item.

        Resource identity is always taken from the persisted source record.
        Backlog item identity is request data: the bounded generic identifier
        scanner accepts an exact structured token but never maps a prefix to a
        project or chooses between multiple sources.
        """
        candidates = self.list(context, resource_type=resource_type)
        search_text = " ".join([request_text, *(str(value) for value in references)])
        source_matches = [
            resource
            for resource in candidates
            if any(
                _contains_identifier(search_text, identifier)
                for identifier in _resource_source_identifiers(resource)
            )
        ]
        if len(source_matches) > 1:
            return ProjectResourceResolution(
                resource=None,
                error=(
                    f"Request identifies multiple '{resource_type}' resources for "
                    f"project '{context.project_id}'; refusing to choose one."
                ),
            )
        if resource_type != BACKLOG_RESOURCE_TYPE:
            return ProjectResourceResolution(
                resource=source_matches[0] if source_matches else None,
                error=None,
            )

        item_candidates = _request_item_identifiers(
            search_text,
            excluded=(
                _resource_source_identifiers(source_matches[0])
                if len(source_matches) == 1
                else ()
            ),
        )
        if len(source_matches) == 1:
            if len(item_candidates) > 1:
                return ProjectResourceResolution(
                    resource=None,
                    error=(
                        f"Request identifies multiple backlog items for project "
                        f"'{context.project_id}'; refusing to choose one."
                    ),
                )
            return ProjectResourceResolution(
                resource=source_matches[0],
                error=None,
                item_id=item_candidates[0] if item_candidates else None,
            )

        if len(candidates) == 1 and len(item_candidates) == 1:
            # A single project backlog source can be paired with one exact
            # request item without any ticket-prefix-to-project inference.
            return ProjectResourceResolution(
                resource=candidates[0], error=None, item_id=item_candidates[0]
            )
        if len(candidates) > 1 and item_candidates:
            return ProjectResourceResolution(
                resource=None,
                error=(
                    f"Request supplies a backlog item but project '{context.project_id}' "
                    "has multiple backlog sources; refusing to choose one."
                ),
            )
        return ProjectResourceResolution(resource=None, error=None)

    def list(
        self,
        context: ProjectContext,
        *,
        resource_type: str | None = None,
    ) -> list[ProjectResource]:
        """List resources for a canonical project, optionally by type."""
        if not isinstance(context, ProjectContext):
            raise TypeError("context must be a canonical ProjectContext.")
        project_id = context.project_id.strip()
        if not project_id:
            raise ValueError("ProjectContext.project_id cannot be empty.")
        if resource_type is not None:
            resource_type = resource_type.strip()
            if not resource_type:
                raise ValueError("resource_type cannot be empty.")

        with self._lock:
            results = self._store.batch(
                [SearchOp(namespace_prefix=_NAMESPACE, limit=1000, offset=0)]
            )[0]
        resources = [
            _item_to_resource(item)
            for item in results or []
            if item.value.get("project_id") == project_id
            and (resource_type is None or item.value.get("resource_type") == resource_type)
        ]
        return sorted(resources, key=lambda resource: resource.updated_at, reverse=True)

    @staticmethod
    def _identity_parts(
        context: ProjectContext,
        resource_type: str,
        location: Mapping[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        if not isinstance(context, ProjectContext):
            raise TypeError("context must be a canonical ProjectContext.")
        project_id = context.project_id.strip()
        if not project_id:
            raise ValueError("ProjectContext.project_id cannot be empty.")
        resource_type = resource_type.strip()
        if not resource_type:
            raise ValueError("resource_type cannot be empty.")
        clean_location = _json_mapping(location, field_name="location")
        if not clean_location:
            raise ValueError("location cannot be empty.")
        return project_id, resource_type, clean_location


_registry: ProjectResourceRegistry | None = None


def get_project_resource_registry() -> ProjectResourceRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectResourceRegistry()
    return _registry


def build_backlog_reference(
    resource: ProjectResource, *, item_id: str | None = None
) -> dict[str, str] | None:
    """Map a validated backlog resource to the existing row-pointer contract.

    A project-level backlog without a request item identifier is not a row
    reference and therefore returns ``None``. The caller must not invent an
    item ID or read one from persisted resource metadata.
    """
    if resource.resource_type != BACKLOG_RESOURCE_TYPE:
        return None

    location = resource.location
    values = {
        "project_key": resource.project_id,
        "spreadsheet_id": location.get("spreadsheet_id"),
        "sheet_name": location.get("sheet_name"),
        "item_id": item_id,
    }
    if not all(isinstance(value, str) and value.strip() for value in values.values()):
        return None
    return {key: value.strip() for key, value in values.items()}


def _resource_identifiers(resource: ProjectResource) -> tuple[str, ...]:
    return _resource_source_identifiers(resource)


def _resource_source_identifiers(resource: ProjectResource) -> tuple[str, ...]:
    if resource.resource_type != BACKLOG_RESOURCE_TYPE:
        return ()
    location = resource.location
    values = (
        location.get("provider"),
        location.get("spreadsheet_id"),
        location.get("sheet_name"),
        location.get("gid"),
    )
    return tuple(str(value).strip() for value in values if value is not None and str(value).strip())


_STRUCTURED_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+)(?![A-Za-z0-9])"
)


def _request_item_identifiers(text: str, *, excluded: Iterable[str] = ()) -> tuple[str, ...]:
    excluded_values = tuple(value for value in excluded if value)
    tokens = list(dict.fromkeys(_STRUCTURED_IDENTIFIER_RE.findall(text)))
    # Prefer structured identifiers containing a numeric component. This is a
    # lexical bound (not a project/ticket-prefix rule) that avoids treating
    # ordinary prose such as "follow-up" as a backlog item. References can
    # still carry a non-numeric exact identifier when no numeric token exists.
    numeric_tokens = [token for token in tokens if any(char.isdigit() for char in token)]
    tokens = numeric_tokens or tokens
    return tuple(
        token
        for token in tokens
        if not any(
            _contains_identifier(token, excluded_value) for excluded_value in excluded_values
        )
    )


def _contains_identifier(text: str, identifier: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(identifier)}(?![A-Za-z0-9_-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None
