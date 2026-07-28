"""Governed runtime skill store for Hub (AGENT-HUB-044).

A Hub skill is an inert procedural instruction Hub's own reasoning can draw on —
never executable code, never a permission/scope/registered-agent change. Skills
live in their own namespace in the shared knowledge store (the same SqliteStore
hub_memory.py already writes learnings into), so this module never touches the
filesystem and can never reach the repository's `.skills/` coding-agent
instructions.

Nothing in the production Hub calls this module yet. AGENT-HUB-020 is responsible
for deciding when /learn should propose a skill and for surfacing find_relevant_skills
into the Hub's own reasoning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from langgraph.store.base import GetOp, PutOp, SearchOp

from .knowledge_store import SqliteStore, get_knowledge_store

SkillStatus = Literal["active", "superseded", "disabled"]

_SKILLS_NAMESPACE = ("hub", "skills")
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 4000


@dataclass(frozen=True)
class HubSkill:
    identifier: str
    slug: str
    title: str
    body: str
    version: int
    status: SkillStatus
    source: str
    created_at: datetime
    supersedes: str | None = None
    memory_id: str | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SkillProposalResult:
    accepted: bool
    skill: HubSkill | None
    reason: str | None = None


def _item_to_skill(item: object) -> HubSkill:
    value = item.value or {}
    return HubSkill(
        identifier=item.key,
        slug=str(value.get("slug", "")),
        title=str(value.get("title", "")),
        body=str(value.get("body", "")),
        version=int(value.get("version", 1)),
        status=str(value.get("status", "active")),
        source=str(value.get("source", "unknown")),
        created_at=item.created_at,
        supersedes=value.get("supersedes"),
        memory_id=value.get("memory_id"),
        evidence=tuple(value.get("evidence", []) or []),
    )


class HubSkillStore:
    def __init__(self, store: SqliteStore | None = None) -> None:
        self._store = store or get_knowledge_store()

    def propose_skill(
        self,
        slug: str,
        title: str,
        body: str,
        *,
        source: str,
        memory_id: str | None = None,
        evidence: Iterable[str] = (),
    ) -> SkillProposalResult:
        """Create a new skill, or a new version of an existing one at `slug`.

        Validates first — nothing is written on rejection. A different slug whose
        title exactly matches (case-insensitively) an already-active skill is
        rejected as a likely duplicate rather than silently creating a near-twin.

        memory_id, when given, is the identifier of the authoritative memory
        record (see hub_memory.LearningRecord) that this skill version came from
        — stored on the version itself so every skill version stays traceable
        back to the specific /learn call that produced it.
        """
        error = self._validate(slug, title, body)
        if error is not None:
            return SkillProposalResult(accepted=False, skill=None, reason=error)

        current = self.get_active_skill(slug)
        if current is None:
            conflict = self._find_active_by_title(title, exclude_slug=slug)
            if conflict is not None:
                return SkillProposalResult(
                    accepted=False,
                    skill=None,
                    reason=(
                        f"Title matches already-active skill '{conflict.slug}' "
                        f"(version {conflict.version}); use that slug to update it "
                        "instead of creating a near-duplicate."
                    ),
                )
            new_skill = self._write_skill(
                slug=slug,
                title=title,
                body=body,
                version=1,
                source=source,
                supersedes=None,
                memory_id=memory_id,
                evidence=evidence,
            )
            return SkillProposalResult(
                accepted=True, skill=new_skill, reason="Created new skill."
            )

        new_skill = self._write_skill(
            slug=slug,
            title=title,
            body=body,
            version=current.version + 1,
            source=source,
            supersedes=current.identifier,
            memory_id=memory_id,
            evidence=evidence,
        )
        self._set_status(current.identifier, "superseded")
        return SkillProposalResult(
            accepted=True,
            skill=new_skill,
            reason=f"Updated skill to version {new_skill.version}.",
        )

    def rollback_skill(self, slug: str) -> HubSkill | None:
        """Demote the active version and reactivate the version it superseded."""
        current = self.get_active_skill(slug)
        if current is None or current.supersedes is None:
            return None

        previous_item = self._store.batch(
            [GetOp(namespace=_SKILLS_NAMESPACE, key=current.supersedes)]
        )[0]
        if previous_item is None:
            return None
        previous = _item_to_skill(previous_item)
        if previous.status != "superseded":
            return None

        self._set_status(current.identifier, "disabled")
        self._set_status(previous.identifier, "active")
        return self.get_active_skill(slug)

    def disable_skill(self, slug: str) -> bool:
        current = self.get_active_skill(slug)
        if current is None:
            return False
        self._set_status(current.identifier, "disabled")
        return True

    def get_active_skill(self, slug: str) -> HubSkill | None:
        for skill in self._all_skills():
            if skill.slug == slug and skill.status == "active":
                return skill
        return None

    def list_active_skills(self) -> list[HubSkill]:
        return [s for s in self._all_skills() if s.status == "active"]

    def find_relevant_skills(self, query: str, *, max_items: int = 5) -> list[HubSkill]:
        """Bounded retrieval: naive keyword overlap over active skills' title+body."""
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t]
        if not terms:
            return []

        scored: list[tuple[int, HubSkill]] = []
        for skill in self.list_active_skills():
            haystack = f"{skill.title} {skill.body}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [skill for _, skill in scored[:max_items]]

    def _all_skills(self) -> list[HubSkill]:
        results = self._store.batch(
            [SearchOp(namespace_prefix=_SKILLS_NAMESPACE, limit=1000, offset=0)]
        )
        return [_item_to_skill(item) for item in (results[0] or [])]

    def _find_active_by_title(self, title: str, *, exclude_slug: str) -> HubSkill | None:
        normalized = title.strip().lower()
        for skill in self.list_active_skills():
            if skill.slug != exclude_slug and skill.title.strip().lower() == normalized:
                return skill
        return None

    def _write_skill(
        self,
        *,
        slug: str,
        title: str,
        body: str,
        version: int,
        source: str,
        supersedes: str | None,
        memory_id: str | None,
        evidence: Iterable[str],
    ) -> HubSkill:
        identifier = f"skill-{uuid4().hex[:8]}"
        payload = {
            "slug": slug,
            "title": title.strip(),
            "body": " ".join(body.split()),
            "version": version,
            "status": "active",
            "source": source,
            "supersedes": supersedes,
            "memory_id": memory_id,
            "evidence": list(evidence),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._store.batch([PutOp(namespace=_SKILLS_NAMESPACE, key=identifier, value=payload)])
        item = self._store.batch([GetOp(namespace=_SKILLS_NAMESPACE, key=identifier)])[0]
        return _item_to_skill(item)

    def _set_status(self, identifier: str, status: SkillStatus) -> None:
        item = self._store.batch([GetOp(namespace=_SKILLS_NAMESPACE, key=identifier)])[0]
        if item is None:
            return
        value = dict(item.value or {})
        value["status"] = status
        self._store.batch([PutOp(namespace=_SKILLS_NAMESPACE, key=identifier, value=value)])

    @staticmethod
    def _validate(slug: str, title: str, body: str) -> str | None:
        if not slug or not _SLUG_PATTERN.match(slug):
            return "Slug must be non-empty, lowercase letters/digits/hyphens only."
        if not title.strip():
            return "Title must not be empty."
        if len(title) > _MAX_TITLE_CHARS:
            return f"Title exceeds {_MAX_TITLE_CHARS} characters."
        if not body.strip():
            return "Body must not be empty."
        if len(body) > _MAX_BODY_CHARS:
            return f"Body exceeds {_MAX_BODY_CHARS} characters."
        return None
