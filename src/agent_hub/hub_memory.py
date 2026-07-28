"""Typed Hub memory: semantic, episodic, and procedural namespaces.

/learn remains Rob's immediate, authoritative command — it always writes an
active, operator-scoped memory record (semantic or procedural) with no approval
gate. HubOrchestrator.learn() additionally runs analyze_learning() as a bounded
LLM call (AGENT-HUB-020) to decide the memory type and whether anything beyond
remembering it should also happen. Only one action_kind — "skill" — actually
executes anything, and only through AGENT-HUB-044's governed HubSkillStore
(hub_skills.py); nothing in this module ever creates a skill, edits docs, files a
backlog item, dispatches code work, or proposes an agent on its own.
documentation/backlog/code_change/new_agent stay text-only proposals until their
own governed action path exists (AGENT-HUB-046).

Automatic semantic extraction (AGENT-HUB-017, see learning_mode.py and
HubOrchestrator.run_learning_pass) writes scope="auto" semantic records
through the same typed storage. Episodic memory (AGENT-HUB-018) is still future
work.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from langgraph.store.base import GetOp, PutOp, SearchOp
from pydantic import BaseModel

from .knowledge_store import SqliteStore, get_knowledge_store

logger = logging.getLogger(__name__)

MemoryType = Literal["semantic", "episodic", "procedural"]
MemoryScope = Literal["operator", "auto"]
MemoryStatus = Literal["active", "pending", "rejected", "disabled"]

MEMORY_TYPES: tuple[MemoryType, ...] = ("semantic", "episodic", "procedural")

# Pre-AGENT-HUB-016 flat namespace. Migrated into the typed semantic namespace
# on first use of HubMemoryManager and never written to again.
_LEGACY_LEARNINGS_NS = ("hub", "learnings")

# Only scope="auto" semantic records are ever compacted — operator-authored
# (/learn) records are never deleted by compaction. Bounding what reaches the
# live prompt is handled separately by format_learnings_for_prompt's budget.
_COMPACTION_TRIGGER_COUNT = 25
_COMPACTION_KEEP_RECENT = 15

_COMPACTION_SYSTEM_PROMPT = (
    "You are compacting an AI agent's stored notes. Merge the following "
    "notes into a single dense summary written as short bullet points. Preserve "
    "every distinct instruction or fact — do not drop information, only remove "
    "redundancy and wording. Respond with only the bullet points, no preamble."
)


def _namespace_for(memory_type: str) -> tuple[str, ...]:
    return ("hub", "memory", memory_type)


@dataclass(frozen=True)
class LearningRecord:
    identifier: str
    value: str
    created_at: datetime
    source: str
    category: str | None = None
    type: MemoryType = "semantic"
    scope: MemoryScope = "operator"
    status: MemoryStatus = "active"
    evidence: tuple[str, ...] = field(default_factory=tuple)


class HubMemoryManager:
    def __init__(
        self,
        store: SqliteStore | None = None,
        *,
        summarizer: Callable[[list[LearningRecord]], str] | None = None,
    ) -> None:
        self._store = store or get_knowledge_store()
        self._summarize = summarizer or _summarize_learnings
        self._migrate_legacy_namespace()

    def learn(self, value: str, *, source: str, category: str | None = None) -> LearningRecord:
        """Rob's explicit, immediately-authoritative instruction. Never gated."""
        record = self._store_record(
            value, source=source, category=category, type="semantic", scope="operator"
        )
        self._compact_if_needed()
        return record

    def learn_procedural(
        self, value: str, *, source: str, category: str | None = None
    ) -> LearningRecord:
        """Same guarantee as learn(), stored as a repeatable rule/habit instead of
        a fact/preference. Still Rob's explicit, immediately-authoritative
        instruction — never gated."""
        record = self._store_record(
            value, source=source, category=category, type="procedural", scope="operator"
        )
        self._compact_if_needed()
        return record

    def record_auto_semantic(
        self, value: str, *, source: str, evidence: Iterable[str] = ()
    ) -> LearningRecord:
        """Store a system-derived (scope=auto) semantic memory, e.g. from
        automatic extraction (AGENT-HUB-017). Subject to compaction, unlike
        operator-authored /learn records.
        """
        record = self._store_record(
            value, source=source, type="semantic", scope="auto", status="active", evidence=evidence
        )
        self._compact_if_needed()
        return record

    def _store_record(
        self,
        value: str,
        *,
        source: str,
        category: str | None = None,
        type: MemoryType = "semantic",
        scope: MemoryScope = "operator",
        status: MemoryStatus = "active",
        evidence: Iterable[str] = (),
    ) -> LearningRecord:
        text = " ".join(value.split())
        if not text:
            raise ValueError("Learning text cannot be empty.")

        identifier = f"mem-{uuid4().hex[:8]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        evidence_tuple = tuple(evidence)
        payload = {
            "value": text,
            "source": source,
            "category": category,
            "type": type,
            "scope": scope,
            "status": status,
            "evidence": list(evidence_tuple),
            "created_at": now_iso,
            "updated_at": now_iso,
        }
        self._store.batch([PutOp(namespace=_namespace_for(type), key=identifier, value=payload)])
        item = self._store.batch([GetOp(namespace=_namespace_for(type), key=identifier)])[0]
        return _item_to_record(item)

    def _compact_if_needed(self) -> None:
        auto_semantic = [
            r for r in self.list_learnings(types=["semantic"]) if r.scope == "auto"
        ]
        if len(auto_semantic) <= _COMPACTION_TRIGGER_COUNT:
            return

        ordered = sorted(auto_semantic, key=lambda r: r.created_at)
        overflow = ordered[: len(ordered) - _COMPACTION_KEEP_RECENT]
        if len(overflow) < 2:
            return

        logger.info(
            "Hub memory: compacting %d overflow auto-semantic record(s) into one summary.",
            len(overflow),
        )
        summary_text = self._summarize(overflow)
        for record in overflow:
            self.forget(record.identifier)
        self._store_record(
            summary_text,
            source="hub-compaction",
            category="compacted-summary",
            type="semantic",
            scope="auto",
            status="active",
            evidence=[r.identifier for r in overflow],
        )

    def list_learnings(self, types: list[str] | None = None) -> list[LearningRecord]:
        """All stored records (any status) across the requested types, or all types."""
        wanted = types or list(MEMORY_TYPES)
        records: list[LearningRecord] = []
        for memory_type in wanted:
            results = self._store.batch(
                [SearchOp(namespace_prefix=_namespace_for(memory_type), limit=1000, offset=0)]
            )
            for item in results[0] or []:
                records.append(_item_to_record(item))
        return records

    def forget(self, identifier: str) -> bool:
        key = identifier.strip()
        if not key:
            return False
        located = self._locate(key)
        if located is None:
            return False
        memory_type, _ = located
        self._store.batch([PutOp(namespace=_namespace_for(memory_type), key=key, value=None)])
        return True

    def set_status(self, identifier: str, status: MemoryStatus) -> bool:
        """Change a record's status in place (e.g. disable a superseded auto fact).

        Never deletes — use forget() for that. Used by supersession (an auto
        semantic record superseding an older one) and, later, procedural
        approve/reject (AGENT-HUB-020).
        """
        key = identifier.strip()
        if not key:
            return False
        located = self._locate(key)
        if located is None:
            return False
        memory_type, item = located
        value = dict(item.value or {})
        value["status"] = status
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._store.batch([PutOp(namespace=_namespace_for(memory_type), key=key, value=value)])
        return True

    def _locate(self, key: str) -> tuple[MemoryType, object] | None:
        for memory_type in MEMORY_TYPES:
            existing = self._store.batch([GetOp(namespace=_namespace_for(memory_type), key=key)])[0]
            if existing is not None:
                return memory_type, existing
        return None

    def _migrate_legacy_namespace(self) -> None:
        """One-time move of pre-typed learnings into the semantic namespace.

        Idempotent: once migrated, the legacy namespace is empty and this is a
        cheap no-op search on every future call.
        """
        results = self._store.batch(
            [SearchOp(namespace_prefix=_LEGACY_LEARNINGS_NS, limit=1000, offset=0)]
        )
        items = results[0] or []
        if not items:
            return

        for item in items:
            value = item.value or {}
            text = str(value.get("value", "")).strip()
            if not text:
                continue
            source = str(value.get("source", "unknown"))
            category = (
                str(value["category"]).strip() if value.get("category") is not None else None
            )
            scope: MemoryScope = "auto" if source == "hub-compaction" else "operator"
            payload = {
                "value": text,
                "source": source,
                "category": category,
                "type": "semantic",
                "scope": scope,
                "status": "active",
                "evidence": [],
                "created_at": item.created_at.isoformat(),
                "updated_at": item.created_at.isoformat(),
                "migrated_from_created_at": item.created_at.isoformat(),
            }
            self._store.batch(
                [
                    PutOp(namespace=_namespace_for("semantic"), key=item.key, value=payload),
                    PutOp(namespace=_LEGACY_LEARNINGS_NS, key=item.key, value=None),
                ]
            )
        logger.info(
            "Hub memory: migrated %d legacy learning(s) into the typed semantic namespace.",
            len(items),
        )


def _item_to_record(item: object) -> LearningRecord:
    value = item.value or {}
    return LearningRecord(
        identifier=item.key,
        value=str(value.get("value", "")).strip(),
        created_at=item.created_at,
        source=str(value.get("source", "unknown")),
        category=(
            str(value["category"]).strip() if value.get("category") is not None else None
        ),
        type=str(value.get("type", "semantic")),
        scope=str(value.get("scope", "operator")),
        status=str(value.get("status", "active")),
        evidence=tuple(value.get("evidence", []) or []),
    )


def _summarize_learnings(records: list[LearningRecord]) -> str:
    """Fold overflow auto-semantic records into one dense summary via the hub's LLM.

    Runs synchronously as part of an explicit /learn call, so this stays
    consistent with the hub's no-silent-background-work design — it never
    fires on its own timer or from passive conversation.
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from .config import DEFAULT_MODEL
    from .cost_log import extract_usage_metadata, record_llm_run

    bullet_list = "\n".join(f"- {r.value} (source: {r.source})" for r in records)
    llm = ChatOpenAI(model=DEFAULT_MODEL, temperature=0)
    usage_cb = UsageMetadataCallbackHandler()
    started = time.perf_counter()
    try:
        response = llm.invoke(
            [
                SystemMessage(content=_COMPACTION_SYSTEM_PROMPT),
                HumanMessage(content=bullet_list),
            ],
            config={"callbacks": [usage_cb]},
        )
        record_llm_run(
            operation="hub_memory_compaction",
            request_kind="compaction",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="ok",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            result_preview=str(response.content)[:200],
        )
        return str(response.content).strip()
    except Exception as exc:
        record_llm_run(
            operation="hub_memory_compaction",
            request_kind="compaction",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="error",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            error=str(exc),
        )
        raise


ExtractionAction = Literal["add", "update", "skip"]
ExtractionConfidence = Literal["high", "medium", "low"]

_EXTRACTION_SYSTEM_PROMPT = (
    "You review a stretch of conversation between an operator (Rob) and Agent "
    "Hub. Decide whether it contains a clear, stable fact, preference, or "
    "correction about Rob or how Hub should behave that is worth remembering "
    "long-term. Do not invent anything not actually said. Do not propose "
    "changes to routing, permissions, budgets, safety rules, prompts, or "
    "code — only personal facts and preferences belong here.\n\n"
    "You are given two kinds of existing remembered facts, each with an id:\n"
    "- Operator-established facts: Rob stated these explicitly via /learn. "
    "They are authoritative and can only be changed by Rob doing that again. "
    "Never propose 'update' against one of these ids. If the conversation "
    "seems to add, duplicate, or conflict with one of these, skip it instead.\n"
    "- Auto-inferred facts: extracted automatically on a previous pass. These "
    "may be superseded by a clearer or corrected version.\n\n"
    "For each candidate fact you find, decide one of:\n"
    "- add: a new fact with no existing match\n"
    "- update: a specific existing AUTO-INFERRED fact (give its id) should be "
    "superseded by this newer/corrected one\n"
    "- skip: not clear, not stable, ambiguous, already covered, or would "
    "duplicate/conflict with an operator-established fact\n\n"
    "Rate your confidence in each non-skip candidate as high, medium, or low. "
    "Only clearly-stated, unambiguous facts should be high confidence. "
    "Return an empty candidate list if there is nothing worth remembering."
)


class _ExtractionCandidateModel(BaseModel):
    action: ExtractionAction
    value: str
    supersedes_id: str | None = None
    confidence: ExtractionConfidence


class _ExtractionResponse(BaseModel):
    candidates: list[_ExtractionCandidateModel] = []


@dataclass(frozen=True)
class ExtractionCandidate:
    action: ExtractionAction
    value: str
    supersedes_id: str | None
    confidence: ExtractionConfidence


def extract_semantic_candidates(
    conversation_text: str,
    existing_active_auto: list[LearningRecord],
    existing_active_operator: list[LearningRecord] = (),
) -> list[ExtractionCandidate]:
    """One bounded LLM call: decide what (if anything) from a quiet session is
    worth remembering automatically.

    Only runs when Learning Mode is on and a session has gone quiet (see
    learning_mode.py) — never per-message, never silently on by default.
    Skip candidates are dropped here; callers should further filter by
    confidence (AGENT-HUB-017: only "high" is auto-stored).

    existing_active_operator (Rob's explicit /learn records) is shown so the
    model can avoid duplicating or contradicting them, but is never a valid
    'update' target — run_learning_pass enforces that regardless of what the
    model proposes.
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from .config import DEFAULT_MODEL
    from .cost_log import extract_usage_metadata, record_llm_run

    operator_block = (
        "\n".join(f"- {r.identifier}: {r.value}" for r in existing_active_operator) or "(none)"
    )
    auto_block = (
        "\n".join(f"- {r.identifier}: {r.value}" for r in existing_active_auto) or "(none yet)"
    )
    human_content = (
        f"Operator-established facts (authoritative; do not update/supersede):\n{operator_block}\n\n"
        f"Auto-inferred facts (may be superseded):\n{auto_block}\n\n"
        f"Recent conversation:\n{conversation_text}"
    )

    llm = ChatOpenAI(model=DEFAULT_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(_ExtractionResponse)
    usage_cb = UsageMetadataCallbackHandler()
    started = time.perf_counter()
    try:
        response = structured_llm.invoke(
            [
                SystemMessage(content=_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=human_content),
            ],
            config={"callbacks": [usage_cb]},
        )
        record_llm_run(
            operation="hub_memory_extraction",
            request_kind="semantic_extraction",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="ok",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            result_preview=str(response)[:200],
        )
    except Exception as exc:
        record_llm_run(
            operation="hub_memory_extraction",
            request_kind="semantic_extraction",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="error",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            error=str(exc),
        )
        raise

    return [
        ExtractionCandidate(
            action=c.action,
            value=c.value.strip(),
            supersedes_id=c.supersedes_id,
            confidence=c.confidence,
        )
        for c in response.candidates
        if c.action != "skip" and c.value.strip()
    ]


LearningMemoryType = Literal["semantic", "procedural"]
LearningActionKind = Literal[
    "memory_only", "skill", "documentation", "backlog", "code_change", "new_agent"
]

_LEARNING_ACTION_LABELS: dict[LearningActionKind, str] = {
    "memory_only": "Memory only",
    "skill": "Skill",
    "documentation": "Documentation change proposed",
    "backlog": "Backlog item proposed",
    "code_change": "Code change proposed",
    "new_agent": "New specialist agent proposed",
}

# action_kind values that only ever produce a text recommendation in this ticket
# (AGENT-HUB-020) — their governed execution path is AGENT-HUB-046, not this one.
_PROPOSAL_ONLY_ACTIONS: frozenset[LearningActionKind] = frozenset(
    {"documentation", "backlog", "code_change", "new_agent"}
)

_LEARNING_ANALYSIS_SYSTEM_PROMPT = (
    "An operator just gave Agent Hub an explicit instruction or lesson via /learn. "
    "It will be stored as an authoritative long-term memory unconditionally, no "
    "matter what you decide here — that part is never gated. Your job is: (1) pick "
    "how it should be remembered, and (2) decide whether anything beyond "
    "remembering it should also happen.\n\n"
    "Restate the lesson in one crisp sentence.\n\n"
    "Pick the memory_type:\n"
    "- semantic: a fact or preference\n"
    "- procedural: a repeatable rule or habit Hub should follow going forward\n\n"
    "Then classify the action_kind — what, if anything, should happen in addition "
    "to remembering it:\n"
    "- memory_only: nothing else to do\n"
    "- skill: a reusable Hub procedure should be created or updated. This is the "
    "only action_kind that actually executes — through a governed skill store, "
    "never by editing any file. You are shown Hub's existing relevant skills; if "
    "one of them is really what's being corrected or extended, reuse its exact "
    "slug (this updates it to a new version) instead of inventing a new one. Only "
    "propose a new skill when none of the shown ones fit. When you choose skill, "
    "also give skill_slug (short, lowercase, hyphenated, stable), skill_title "
    "(human label), and skill_body (the actual procedure text Hub should follow).\n"
    "- documentation: project docs should be updated to reflect this\n"
    "- backlog: this describes a bug or gap that belongs on the backlog\n"
    "- code_change: this requires a runtime code change\n"
    "- new_agent: this needs a new specialist agent that doesn't exist yet\n\n"
    "documentation, backlog, code_change, and new_agent are proposals only — Hub "
    "cannot and must not perform them itself here. Give a one-sentence, concrete "
    "suggestion for whichever action_kind you chose (skill_body covers that role "
    "when action_kind is skill). State whether a code change is needed regardless "
    "of the chosen category, since a documentation, skill, or backlog suggestion "
    "can still imply one.\n\n"
    "You are shown existing operator-established facts, Hub's existing relevant "
    "skills, and relevant authoritative documentation, so you don't recommend or "
    "duplicate something already known, already a skill, or already documented."
)


class _LearningAnalysisModel(BaseModel):
    restated_lesson: str
    memory_type: LearningMemoryType
    action_kind: LearningActionKind
    suggestion: str
    code_change_needed: bool
    skill_slug: str | None = None
    skill_title: str | None = None
    skill_body: str | None = None


@dataclass(frozen=True)
class LearningAnalysis:
    restated_lesson: str
    memory_type: LearningMemoryType
    action_kind: LearningActionKind
    suggestion: str
    code_change_needed: bool
    skill_slug: str | None = None
    skill_title: str | None = None
    skill_body: str | None = None


def analyze_learning(
    value: str,
    existing_operator: list[LearningRecord] = (),
    relevant_skills: list = (),
    relevant_docs: list = (),
) -> LearningAnalysis:
    """One bounded LLM call: given an operator's /learn text, decide how to
    remember it (memory_type) and whether anything beyond remembering it should
    also happen (action_kind).

    Runs synchronously as part of every explicit /learn call. Only action_kind
    "skill" executes anything (via AGENT-HUB-044's governed skill store, called
    by HubOrchestrator.learn() — this function never writes anything itself).
    documentation/backlog/code_change/new_agent are always proposals only here,
    matching Hub's no-automatic-code-change and human-approves rules; their
    governed execution path is AGENT-HUB-046.

    relevant_skills and relevant_docs are hub_skills.HubSkill / hub_context.
    ContextSource instances (bounded retrieval already applied by the caller) —
    typed loosely here to avoid a hard import dependency in this module.
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from .config import DEFAULT_MODEL
    from .cost_log import extract_usage_metadata, record_llm_run

    operator_block = "\n".join(f"- {r.value}" for r in existing_operator) or "(none)"
    skills_block = (
        "\n".join(f"- {s.slug}: {s.title} — {s.body}" for s in relevant_skills) or "(none)"
    )
    docs_block = (
        "\n".join(f"- {d.identifier}: {d.content}" for d in relevant_docs) or "(none)"
    )
    human_content = (
        f"Existing operator-established facts:\n{operator_block}\n\n"
        f"Hub's existing relevant skills:\n{skills_block}\n\n"
        f"Relevant authoritative documentation:\n{docs_block}\n\n"
        f"New lesson:\n{value}"
    )

    llm = ChatOpenAI(model=DEFAULT_MODEL, temperature=0)
    structured_llm = llm.with_structured_output(_LearningAnalysisModel)
    usage_cb = UsageMetadataCallbackHandler()
    started = time.perf_counter()
    try:
        response = structured_llm.invoke(
            [
                SystemMessage(content=_LEARNING_ANALYSIS_SYSTEM_PROMPT),
                HumanMessage(content=human_content),
            ],
            config={"callbacks": [usage_cb]},
        )
        record_llm_run(
            operation="hub_memory_learning_analysis",
            request_kind="learning_analysis",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="ok",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            result_preview=str(response)[:200],
        )
    except Exception as exc:
        record_llm_run(
            operation="hub_memory_learning_analysis",
            request_kind="learning_analysis",
            requested_model=DEFAULT_MODEL,
            effective_model=DEFAULT_MODEL,
            status="error",
            duration_seconds=time.perf_counter() - started,
            usage_by_model=extract_usage_metadata(usage_cb),
            error=str(exc),
        )
        raise

    return LearningAnalysis(
        restated_lesson=response.restated_lesson.strip(),
        memory_type=response.memory_type,
        action_kind=response.action_kind,
        suggestion=response.suggestion.strip(),
        code_change_needed=response.code_change_needed,
        skill_slug=(response.skill_slug or "").strip() or None,
        skill_title=(response.skill_title or "").strip() or None,
        skill_body=(response.skill_body or "").strip() or None,
    )


def format_learning_list(records: list[LearningRecord]) -> str:
    if not records:
        return "No hub learnings have been stored yet."

    lines = ["Stored hub learnings:"]
    for record in records:
        detail = (
            f"{record.identifier} | "
            f"{record.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} "
            f"| source={record.source} | type={record.type} | status={record.status}"
        )
        if record.category:
            detail += f" | category={record.category}"
        lines.append(detail)
        lines.append(f"  {record.value}")
    return "\n".join(lines)


def format_learnings_for_prompt(
    records: list[LearningRecord],
    *,
    max_items: int = 20,
    max_chars: int = 3000,
) -> str:
    """Render active learnings for injection into the orchestrator system prompt.

    Pending, rejected, and disabled records are never injected. Operator
    records (Rob's explicit /learn) always rank ahead of auto-inferred ones
    regardless of recency, so a newer automatic guess can never crowd out an
    older explicit instruction — within each scope, most-recent is prioritized.
    The output is capped so a growing learning store cannot unboundedly
    inflate every LLM call.
    """
    active = [r for r in records if r.status == "active"]
    if not active:
        return ""

    operator = sorted(
        (r for r in active if r.scope == "operator"), key=lambda r: r.created_at, reverse=True
    )
    auto = sorted(
        (r for r in active if r.scope == "auto"), key=lambda r: r.created_at, reverse=True
    )
    ordered = operator + auto
    candidates = ordered[:max_items]

    included_operator: list[str] = []
    included_auto: list[str] = []
    total_chars = 0
    for record in candidates:
        line = f"- {record.value} (source: {record.source})"
        if total_chars + len(line) + 1 > max_chars:
            break
        if record.scope == "operator":
            included_operator.append(line)
        else:
            included_auto.append(line)
        total_chars += len(line) + 1

    if not included_operator and not included_auto:
        return ""

    omitted = len(ordered) - len(included_operator) - len(included_auto)

    sections: list[str] = []
    if included_operator:
        sections.append(
            "Operator-established hub learnings (Rob's explicit instructions — "
            "authoritative; apply these when relevant):\n" + "\n".join(included_operator)
        )
    if included_auto:
        sections.append(
            "Auto-inferred hub learnings (lower confidence; where these conflict "
            "with the operator-established learnings above, the operator ones "
            "win):\n" + "\n".join(included_auto)
        )
    text = "\n\n".join(sections)
    if omitted:
        text += f"\n\n(...{omitted} older learning(s) omitted; use /memory to view all.)"
    return text


def format_learning_confirmation(
    record: LearningRecord,
    *,
    analysis: LearningAnalysis | None = None,
    analysis_error: str | None = None,
    relevant_skills: list = (),
    relevant_docs: list = (),
    skill_result: object | None = None,
) -> str:
    if analysis_error is not None:
        return (
            "Learned: (analysis unavailable)\n"
            f"Stored: {record.identifier} (semantic, default) from {record.source}: "
            f"{record.value}\n"
            f"Evidence checked: not performed — analysis failed: {analysis_error}\n"
            "Destination/action: Memory only (fallback)\n"
            "Validation: N/A\n"
            "Approval required: No"
        )

    if analysis is None:
        return f"Stored learning {record.identifier} from {record.source}: {record.value}"

    skills_ids = ", ".join(s.slug for s in relevant_skills) or "none"
    docs_ids = ", ".join(d.identifier for d in relevant_docs) or "none"
    evidence_line = f"Evidence checked: skills=[{skills_ids}] docs=[{docs_ids}]"

    if analysis.action_kind == "skill":
        if skill_result is not None and skill_result.accepted:
            destination = f"Skill — {skill_result.reason}"
            validation = f"Passed — active (version {skill_result.skill.version})"
        elif skill_result is not None:
            destination = f"Skill — proposal rejected: {skill_result.reason}"
            validation = f"Rejected: {skill_result.reason}"
        else:
            destination = "Skill — no proposal executed"
            validation = "N/A"
        approval_required = "No"
    elif analysis.action_kind in _PROPOSAL_ONLY_ACTIONS:
        destination = f"{_LEARNING_ACTION_LABELS[analysis.action_kind]}: {analysis.suggestion}"
        validation = "N/A — proposal only, not executed"
        approval_required = "Yes"
    else:
        destination = _LEARNING_ACTION_LABELS[analysis.action_kind]
        validation = "N/A"
        approval_required = "No"

    return (
        f"Learned: {analysis.restated_lesson}\n"
        f"Stored: {record.identifier} ({analysis.memory_type}) from {record.source}: "
        f"{record.value}\n"
        f"{evidence_line}\n"
        f"Destination/action: {destination}\n"
        f"Validation: {validation}\n"
        f"Approval required: {approval_required}"
    )


def format_forget_confirmation(identifier: str) -> str:
    return f"Forgot learning {identifier}."
