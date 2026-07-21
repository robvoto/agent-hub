"""Typed Hub memory: semantic, episodic, and procedural namespaces.

/learn remains Rob's immediate, authoritative command — it always writes an
active, operator-scoped semantic record with no approval gate.

Automatic semantic extraction (HUB-LEARN-002, see learning_mode.py and
HubOrchestrator.run_learning_pass) writes scope="auto" semantic records
through the same typed storage. Episodic curation and procedural proposals
(HUB-LEARN-003/005) are still future work.
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

# Pre-HUB-LEARN-001 flat namespace. Migrated into the typed semantic namespace
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
        automatic extraction (HUB-LEARN-002). Subject to compaction, unlike
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
        approve/reject (HUB-LEARN-005).
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
    "You are given the existing remembered facts (each with an id). For each "
    "candidate fact you find, decide one of:\n"
    "- add: a new fact with no existing match\n"
    "- update: a specific existing fact (give its id) should be superseded by "
    "this newer/corrected one\n"
    "- skip: not clear, not stable, ambiguous, contradictory, or already "
    "covered by an existing fact\n\n"
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
) -> list[ExtractionCandidate]:
    """One bounded LLM call: decide what (if anything) from a quiet session is
    worth remembering automatically.

    Only runs when Learning Mode is on and a session has gone quiet (see
    learning_mode.py) — never per-message, never silently on by default.
    Skip candidates are dropped here; callers should further filter by
    confidence (HUB-LEARN-002: only "high" is auto-stored).
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    from .config import DEFAULT_MODEL
    from .cost_log import extract_usage_metadata, record_llm_run

    existing_block = (
        "\n".join(f"- {r.identifier}: {r.value}" for r in existing_active_auto) or "(none yet)"
    )
    human_content = (
        f"Existing remembered facts:\n{existing_block}\n\nRecent conversation:\n{conversation_text}"
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

    Pending, rejected, and disabled records are never injected. Most-recent
    active learnings are prioritized and the output is capped so a growing
    learning store cannot unboundedly inflate every LLM call.
    """
    active = [r for r in records if r.status == "active"]
    if not active:
        return ""

    ordered = sorted(active, key=lambda r: r.created_at, reverse=True)
    candidates = ordered[:max_items]

    included: list[str] = []
    total_chars = 0
    for record in candidates:
        line = f"- {record.value} (source: {record.source})"
        if total_chars + len(line) + 1 > max_chars:
            break
        included.append(line)
        total_chars += len(line) + 1

    if not included:
        return ""

    omitted = len(ordered) - len(included)
    text = "Operator-established hub learnings (apply these when relevant):\n" + "\n".join(
        included
    )
    if omitted:
        text += f"\n(...{omitted} older learning(s) omitted; use /memory to view all.)"
    return text


def format_learning_confirmation(record: LearningRecord) -> str:
    return f"Stored learning {record.identifier} from {record.source}: {record.value}"


def format_forget_confirmation(identifier: str) -> str:
    return f"Forgot learning {identifier}."
