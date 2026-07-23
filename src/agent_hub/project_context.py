"""Per-session 'current project' selection for cross-project specialist dispatch.

Hub does not decide which projects a subprocess specialist is allowed to
touch — that allowlist is enforced server-side by the specialist itself
(e.g. ai-tech-lead's own allowed_project_roots). Hub only remembers which
project the operator picked with /project, so it can pass project_root
through on dispatch instead of always defaulting to the specialist's own
project.

Persisted in the Hub knowledge store, keyed by session_id, so a hub restart
does not silently revert an operator's /project selection now that session_id
itself survives a restart (see AGENT-HUB-032).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from langgraph.store.base import GetOp, PutOp

from .knowledge_store import get_knowledge_store

_NAMESPACE = ("hub", "project_context")


class ProjectContextRegistry:
    def __init__(self, store: Any = None) -> None:
        self._lock = threading.Lock()
        self._store = store or get_knowledge_store()

    def get(self, session_id: str) -> str | None:
        with self._lock:
            item = self._store.batch([GetOp(namespace=_NAMESPACE, key=session_id)])[0]
        return item.value.get("path") if item is not None else None

    def set(self, session_id: str, path: str) -> str:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"'{path}' is not a directory.")
        with self._lock:
            self._store.batch(
                [PutOp(namespace=_NAMESPACE, key=session_id, value={"path": str(resolved)})]
            )
        return str(resolved)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.batch([PutOp(namespace=_NAMESPACE, key=session_id, value=None)])


_registry: ProjectContextRegistry | None = None


def get_project_context_registry() -> ProjectContextRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectContextRegistry()
    return _registry
