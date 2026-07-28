"""Bounded read-only context service for Hub's own runtime learning (AGENT-HUB-045).

Exposes an explicit, small, approved set of Hub documentation, live specialist
manifests, and runtime metadata — read-only, never the whole repository. Every
lookup takes a caller-supplied identifier that is checked against a fixed
allowlist; there is no code path that accepts an arbitrary filesystem path, so
this module cannot be used to read outside its approved set, and it never writes
anything.

Nothing in the production Hub calls this module yet. AGENT-HUB-020 is responsible
for deciding when /learn's analysis should consult it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .config import AGENT_REGISTRY_DIR, DEFAULT_MODEL, PROJECT_ROOT
from .registry import load_registry_report, spec_fingerprint

ContextSourceKind = Literal["documentation", "manifest", "runtime_metadata"]

# The approved documentation set, per docs/INDEX.md's "Core" section plus the
# repo's always-loaded AGENTS.md. Expanding this is a content decision (add a
# row here), not a new capability — deliberately small and explicit rather than
# "read whatever exists under docs/".
_APPROVED_DOCS: tuple[str, ...] = (
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/ARCHITECTURE.md",
    "docs/COMMANDS.md",
    "docs/TELEGRAM_MVP_VALIDATION.md",
)

_MAX_DOC_CHARS = 20_000
_SNIPPET_WINDOW_CHARS = 250


@dataclass(frozen=True)
class ContextSource:
    identifier: str
    kind: ContextSourceKind
    content: str
    freshness: str  # ISO8601: file mtime for docs, read-time for live-loaded sources


@dataclass(frozen=True)
class ContextLookupResult:
    available: bool
    source: ContextSource | None
    reason: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HubContextService:
    def __init__(
        self, project_root: Path | None = None, registry_dir: Path | None = None
    ) -> None:
        self._project_root = project_root or PROJECT_ROOT
        self._registry_dir = registry_dir or AGENT_REGISTRY_DIR

    # -- documentation -----------------------------------------------------

    def list_documentation_sources(self) -> tuple[str, ...]:
        return _APPROVED_DOCS

    def read_documentation(self, identifier: str) -> ContextLookupResult:
        if identifier not in _APPROVED_DOCS:
            return ContextLookupResult(
                available=False,
                source=None,
                reason=f"'{identifier}' is not an approved documentation source.",
            )

        path = self._project_root / identifier
        if not path.is_file():
            return ContextLookupResult(
                available=False,
                source=None,
                reason=f"Approved source '{identifier}' is missing on disk at {path}.",
            )

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return ContextLookupResult(
                available=False, source=None, reason=f"Could not read '{identifier}': {exc}"
            )

        if len(text) > _MAX_DOC_CHARS:
            text = text[:_MAX_DOC_CHARS] + "\n...(truncated)"

        freshness = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        return ContextLookupResult(
            available=True,
            source=ContextSource(
                identifier=identifier, kind="documentation", content=text, freshness=freshness
            ),
        )

    def find_relevant_documentation(self, query: str, *, max_items: int = 3) -> list[ContextSource]:
        """Bounded retrieval: keyword overlap over the approved doc set, each
        result trimmed to a small window around its strongest match rather than
        the whole file."""
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t]
        if not terms:
            return []

        scored: list[tuple[int, str, str]] = []
        for identifier in _APPROVED_DOCS:
            result = self.read_documentation(identifier)
            if not result.available or result.source is None:
                continue
            lowered = result.source.content.lower()
            score = sum(lowered.count(term) for term in terms)
            if score > 0:
                scored.append((score, identifier, result.source.content))

        scored.sort(key=lambda item: item[0], reverse=True)

        results: list[ContextSource] = []
        for score, identifier, content in scored[:max_items]:
            snippet = _best_snippet(content, terms)
            source = self.read_documentation(identifier).source
            assert source is not None
            results.append(
                ContextSource(
                    identifier=identifier,
                    kind="documentation",
                    content=snippet,
                    freshness=source.freshness,
                )
            )
        return results

    # -- manifests -----------------------------------------------------------

    def list_manifests(self) -> list[ContextSource]:
        report = load_registry_report(self._registry_dir)
        freshness = _now_iso()
        return [
            ContextSource(
                identifier=f"manifest:{spec.id}",
                kind="manifest",
                content=(
                    f"{spec.name} ({spec.id}) v{spec.version}: {spec.purpose} "
                    f"[fingerprint={spec_fingerprint(spec)}]"
                ),
                freshness=freshness,
            )
            for spec in report.specs
        ]

    def get_manifest(self, agent_id: str) -> ContextLookupResult:
        for source in self.list_manifests():
            if source.identifier == f"manifest:{agent_id}":
                return ContextLookupResult(available=True, source=source)
        return ContextLookupResult(
            available=False,
            source=None,
            reason=f"No loaded, valid manifest for agent id '{agent_id}'.",
        )

    # -- runtime metadata ------------------------------------------------------

    def runtime_metadata(self) -> ContextSource:
        report = load_registry_report(self._registry_dir)
        error_lines = "; ".join(f"{e.source}: {e.message}" for e in report.errors) or "none"
        content = (
            f"default_model={DEFAULT_MODEL}; registry_dir={self._registry_dir}; "
            f"loaded_agents={len(report.specs)}; invalid_manifests={len(report.errors)} "
            f"({error_lines})"
        )
        return ContextSource(
            identifier="runtime:registry-health",
            kind="runtime_metadata",
            content=content,
            freshness=_now_iso(),
        )


def _best_snippet(content: str, terms: list[str]) -> str:
    lowered = content.lower()
    best_index = -1
    for term in terms:
        idx = lowered.find(term)
        if idx != -1 and (best_index == -1 or idx < best_index):
            best_index = idx
    if best_index == -1:
        return content[:_SNIPPET_WINDOW_CHARS]

    start = max(0, best_index - _SNIPPET_WINDOW_CHARS // 2)
    end = min(len(content), best_index + _SNIPPET_WINDOW_CHARS // 2)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(content) else ""
    return f"{prefix}{content[start:end]}{suffix}"
