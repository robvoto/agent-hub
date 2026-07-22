"""Per-session 'current project' selection for cross-project specialist dispatch.

Hub does not decide which projects a subprocess specialist is allowed to
touch — that allowlist is enforced server-side by the specialist itself
(e.g. ai-tech-lead's own allowed_project_roots). Hub only remembers which
project the operator picked with /project, so it can pass project_root
through on dispatch instead of always defaulting to the specialist's own
project.

In-memory only, keyed by session_id: a hub restart loses the selection,
same accepted tradeoff as learning_mode.py.
"""

from __future__ import annotations

import threading
from pathlib import Path


class ProjectContextRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, str] = {}

    def get(self, session_id: str) -> str | None:
        with self._lock:
            return self._current.get(session_id)

    def set(self, session_id: str, path: str) -> str:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"'{path}' is not a directory.")
        with self._lock:
            self._current[session_id] = str(resolved)
        return str(resolved)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._current.pop(session_id, None)


_registry: ProjectContextRegistry | None = None


def get_project_context_registry() -> ProjectContextRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectContextRegistry()
    return _registry
