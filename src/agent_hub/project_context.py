"""Per-session canonical project context for cross-project specialist dispatch.

Hub does not decide which projects a subprocess specialist is allowed to
touch — that allowlist is enforced server-side by the specialist itself
(e.g. ai-tech-lead's own allowed_project_roots). Hub remembers which project
the operator picked with /project, resolves it to a canonical
`ProjectContext` (a stable `project_id`, a contract version, a fingerprint
over that identity, and light metadata), and revalidates that context
against the filesystem immediately before every fresh specialist dispatch
that would receive it — so a project that moved, was deleted, or now
resolves to a different identity than when it was selected stops the
dispatch with a clear error instead of silently sending a stale path
(AGENT-HUB-039). A specialist that hasn't opted into the richer fields still
gets a plain `project_root`; the canonical fields are additive.

Persisted in the Hub knowledge store, keyed by session_id, so a hub restart
does not silently revert an operator's /project selection now that session_id
itself survives a restart (see AGENT-HUB-032).
"""

from __future__ import annotations

import configparser
import hashlib
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langgraph.store.base import GetOp, PutOp

from .knowledge_store import get_knowledge_store

_NAMESPACE = ("hub", "project_context")

PROJECT_CONTRACT_VERSION = 1
"""Schema version of the `ProjectContext` Hub persists and dispatches. Bump
only on a breaking change to `ProjectContext`'s fields."""


@dataclass(frozen=True)
class ProjectContext:
    """Hub's canonical identity for a selected project.

    `project_id` is derived from the project's git remote when one exists
    (stable across clones and local path layout differences) and falls back
    to the resolved absolute path otherwise — Hub does not maintain a
    separate registry of known projects; identity is derived, not assigned.
    `fingerprint` is a hash over `project_id` + the resolved root + the git
    remote (if any). Hub recomputes it fresh immediately before every
    fresh dispatch and compares it to the persisted value: a mismatch means
    the root now resolves to a different project than when it was selected.
    """

    project_id: str
    root: str
    contract_version: int
    fingerprint: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectContext":
        return cls(
            project_id=data["project_id"],
            root=data["root"],
            contract_version=data["contract_version"],
            fingerprint=data["fingerprint"],
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class ProjectContextResolution:
    """Result of revalidating a persisted `ProjectContext` before dispatch.

    `error` is set (and `context` is None) when a project was selected but
    no longer resolves cleanly — the caller must stop the dispatch rather
    than fall back to the stale value. Both `None` means nothing was
    selected, which is a valid state: the specialist uses its own default.
    """

    context: ProjectContext | None
    error: str | None


def _git_remote_url(root: Path) -> str | None:
    """Read the `origin` remote URL straight out of `.git/config`.

    Deliberately not a `git` subprocess call: this runs on every /project
    selection and every dispatch's fresh revalidation, so it needs to be
    fast and dependency-free, and it must not share any process-spawning
    machinery with specialist dispatch (which tests routinely fake at the
    `subprocess.Popen` level).
    """
    git_dir = root / ".git"
    if git_dir.is_file():
        try:
            pointer = git_dir.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not pointer.startswith("gitdir:"):
            return None
        candidate = Path(pointer.split(":", 1)[1].strip())
        git_dir = candidate if candidate.is_absolute() else (root / candidate)

    config_file = git_dir / "config"
    if not config_file.is_file():
        return None

    parser = configparser.ConfigParser()
    try:
        parser.read(config_file, encoding="utf-8")
    except configparser.Error:
        return None
    for section in parser.sections():
        if section.strip() == 'remote "origin"':
            url = parser.get(section, "url", fallback=None)
            if url and url.strip():
                return url.strip()
    return None


def _derive_project_id(root: Path, *, remote_url: str | None) -> str:
    if remote_url:
        return remote_url[:-4] if remote_url.endswith(".git") else remote_url
    return str(root)


def _fingerprint(project_id: str, root: Path, remote_url: str | None) -> str:
    payload = f"{project_id}|{root}|{remote_url or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_project_context(root: Path) -> ProjectContext:
    remote_url = _git_remote_url(root)
    project_id = _derive_project_id(root, remote_url=remote_url)
    return ProjectContext(
        project_id=project_id,
        root=str(root),
        contract_version=PROJECT_CONTRACT_VERSION,
        fingerprint=_fingerprint(project_id, root, remote_url),
        metadata={
            "name": root.name,
            "vcs": "git" if (remote_url or (root / ".git").exists()) else "none",
            "remote": remote_url,
        },
    )


class ProjectContextRegistry:
    def __init__(self, store: Any = None) -> None:
        self._lock = threading.Lock()
        self._store = store or get_knowledge_store()

    def get(self, session_id: str) -> ProjectContext | None:
        with self._lock:
            item = self._store.batch([GetOp(namespace=_NAMESPACE, key=session_id)])[0]
        if item is None or not item.value:
            return None
        return ProjectContext.from_dict(item.value)

    def set(self, session_id: str, path: str) -> ProjectContext:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"'{path}' is not a directory.")
        context = _build_project_context(resolved)
        with self._lock:
            self._store.batch(
                [PutOp(namespace=_NAMESPACE, key=session_id, value=context.to_dict())]
            )
        return context

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.batch([PutOp(namespace=_NAMESPACE, key=session_id, value=None)])

    def resolve_for_dispatch(self, session_id: str) -> ProjectContextResolution:
        """Revalidate the persisted selection against the filesystem right now.

        Called immediately before a fresh (non-resumed) dispatch that would
        pass project context to a specialist. A resumed dispatch never calls
        this — it replays the exact context pinned at the original dispatch
        instead (see `orchestrator._dispatch_subprocess`'s
        `project_context_override`), so a task already in flight isn't
        broken by a selection change made after it started.
        """
        stored = self.get(session_id)
        if stored is None:
            return ProjectContextResolution(context=None, error=None)

        root = Path(stored.root)
        if not root.is_dir():
            return ProjectContextResolution(
                context=None,
                error=(
                    f"Selected project '{stored.project_id}' no longer exists at "
                    f"{stored.root}. Set a new one with /project <path>."
                ),
            )

        fresh = _build_project_context(root)
        if fresh.project_id != stored.project_id or fresh.fingerprint != stored.fingerprint:
            return ProjectContextResolution(
                context=None,
                error=(
                    f"Selected project '{stored.project_id}' at {stored.root} no longer "
                    "matches what was selected — its identity changed underneath it "
                    "(e.g. a different repository is now there). "
                    "Re-run /project <path> to reselect it."
                ),
            )
        return ProjectContextResolution(context=stored, error=None)


_registry: ProjectContextRegistry | None = None


def get_project_context_registry() -> ProjectContextRegistry:
    global _registry
    if _registry is None:
        _registry = ProjectContextRegistry()
    return _registry
