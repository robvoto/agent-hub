"""Typed Hub memory: semantic, episodic, and procedural namespaces.

/learn remains Rob's immediate, authoritative command — it always writes an
active, operator-scoped semantic record with no approval gate. HubOrchestrator.
learn() additionally runs analyze_learning() as a bounded LLM call to recommend
what else (if anything) should follow — a skill, doc, backlog, or code change —
but that analysis never gates or alters the memory write, and it only
recommends; nothing here creates a skill, edits docs, files a backlog item, or
changes code on its own.

Automatic semantic extraction (AGENT-HUB-017, see learning_mode.py and
HubOrchestrator.run_learning_pass) writes scope="auto" semantic records
through the same typed storage. Episodic curation and procedural proposals
(AGENT-HUB-018/005) are still future work.
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


LearningActionKind = Literal["memory_only", "skill", "documentation", "backlog", "code_change"]

_LEARNING_ACTION_LABELS: dict[LearningActionKind, str] = {
    "memory_only": "No further action — remembering this is enough.",
    "skill": "A reusable skill should be created or updated.",
    "documentation": "Project documentation should be updated.",
    "backlog": "This is a bug or gap worth a backlog item.",
    "code_change": "This likely needs a runtime code change.",
}

_LEARNING_ANALYSIS_SYSTEM_PROMPT = (
    "An operator just gave Agent Hub an explicit instruction or lesson via /learn. "
    "It has already been stored as an authoritative long-term memory unconditionally — "
    "nothing you decide here changes that. Your only job is to say what, if anything, "
    "should happen next.\n\n"
    "Restate the lesson in one crisp sentence.\n\n"
    "Then classify the best next action:\n"
    "- memory_only: remembering it is enough, nothing else to do\n"
    "- skill: a reusable skill/procedure should be created or updated to reflect this\n"
    "- documentation: project docs should be updated to reflect this\n"
    "- backlog: this describes a bug or gap that belongs on the backlog\n"
    "- code_change: this requires a runtime code change\n\n"
    "Give a one-sentence, concrete suggestion for that action. Never take the action "
    "yourself — only recommend it; a human decides and executes separately. State "
    "whether a code change is needed regardless of the chosen category, since a "
    "documentation, skill, or backlog suggestion can still imply one.\n\n"
    "You are shown existing operator-established facts so you don't recommend "
    "something already known and stored."
)


class _LearningAnalysisModel(BaseModel):
    restated_lesson: str
    action_kind: LearningActionKind
    suggestion: str
    code_change_needed: bool


@dataclass(frozen=True)
class LearningAnalysis:
    restated_lesson: str
    action_kind: LearningActionKind
    suggestion: str
    code_change_needed: bool


def analyze_learning(
    value: str, existing_operator: list[LearningRecord] = ()
) -> LearningAnalysis:
    """One bounded LLM call: given an operator's /learn text, recommend what (if
    anything) beyond storing it in memory should happen next.

    Runs synchronously as part of every explicit /learn call — this is analysis,
    not gated automation. It never creates a skill, edits docs, files a backlog
    item, or changes code; it only recommends, matching Hub's no-automatic-code-
    change and human-approves-code-changes rules.
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from .config import DEFAULT_MODEL
    from .cost_log import extract_usage_metadata, record_llm_run

    operator_block = (
        "\n".join(f"- {r.value}" for r in existing_operator) or "(none)"
    )
    human_content = (
        f"Existing operator-established facts:\n{operator_block}\n\nNew lesson:\n{value}"
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
        action_kind=response.action_kind,
        suggestion=response.suggestion.strip(),
        code_change_needed=response.code_change_needed,
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
) -> str:
    stored = f"Stored learning {record.identifier} from {record.source}: {record.value}"
    if analysis is not None:
        return (
            f"{stored}\n\n"
            f"Learned: {analysis.restated_lesson}\n"
            f"Action: {_LEARNING_ACTION_LABELS[analysis.action_kind]}\n"
            f"Suggestion: {analysis.suggestion}\n"
            f"Code change: {'Yes' if analysis.code_change_needed else 'No'}"
        )
    if analysis_error is not None:
        return f"{stored}\n\n(Could not analyze this lesson for a recommended action: {analysis_error})"
    return stored


def format_forget_confirmation(identifier: str) -> str:
    return f"Forgot learning {identifier}."
