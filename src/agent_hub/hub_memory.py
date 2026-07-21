"""Explicit operator-managed Hub learnings."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from langgraph.store.base import GetOp, PutOp, SearchOp

from .knowledge_store import SqliteStore, get_knowledge_store

logger = logging.getLogger(__name__)

_LEARNINGS_NS = ("hub", "learnings")

# Once stored learnings exceed this count, the oldest overflow is compacted
# into a single summary so the store stays bounded without silently dropping
# older learnings from ever influencing the hub again (see format_learnings_for_prompt).
_COMPACTION_TRIGGER_COUNT = 25
_COMPACTION_KEEP_RECENT = 15

_COMPACTION_SYSTEM_PROMPT = (
    "You are compacting an AI agent's stored operator notes. Merge the following "
    "notes into a single dense summary written as short bullet points. Preserve "
    "every distinct instruction or fact — do not drop information, only remove "
    "redundancy and wording. Respond with only the bullet points, no preamble."
)


@dataclass(frozen=True)
class LearningRecord:
    identifier: str
    value: str
    created_at: datetime
    source: str
    category: str | None = None


class HubMemoryManager:
    def __init__(
        self,
        store: SqliteStore | None = None,
        *,
        summarizer: Callable[[list[LearningRecord]], str] | None = None,
    ) -> None:
        self._store = store or get_knowledge_store()
        self._summarize = summarizer or _summarize_learnings

    def learn(self, value: str, *, source: str, category: str | None = None) -> LearningRecord:
        record = self._store_record(value, source=source, category=category)
        self._compact_if_needed()
        return record

    def _store_record(
        self, value: str, *, source: str, category: str | None = None
    ) -> LearningRecord:
        text = " ".join(value.split())
        if not text:
            raise ValueError("Learning text cannot be empty.")

        identifier = f"mem-{uuid4().hex[:8]}"
        created_at = datetime.now(timezone.utc)
        payload = {
            "value": text,
            "source": source,
            "category": category,
            "created_at": created_at.isoformat(),
        }
        self._store.batch([PutOp(namespace=_LEARNINGS_NS, key=identifier, value=payload)])
        return LearningRecord(
            identifier=identifier,
            value=text,
            created_at=created_at,
            source=source,
            category=category,
        )

    def _compact_if_needed(self) -> None:
        records = self.list_learnings()
        if len(records) <= _COMPACTION_TRIGGER_COUNT:
            return

        ordered = sorted(records, key=lambda r: r.created_at)
        overflow = ordered[: len(ordered) - _COMPACTION_KEEP_RECENT]
        if len(overflow) < 2:
            return

        logger.info(
            "Hub memory: compacting %d overflow learning(s) into one summary.",
            len(overflow),
        )
        summary_text = self._summarize(overflow)
        for record in overflow:
            self.forget(record.identifier)
        self._store_record(
            summary_text,
            source="hub-compaction",
            category="compacted-summary",
        )

    def list_learnings(self) -> list[LearningRecord]:
        results = self._store.batch(
            [SearchOp(namespace_prefix=_LEARNINGS_NS, limit=1000, offset=0)]
        )
        items = results[0] or []
        records: list[LearningRecord] = []
        for item in items:
            value = item.value or {}
            records.append(
                LearningRecord(
                    identifier=item.key,
                    value=str(value.get("value", "")).strip(),
                    created_at=item.created_at,
                    source=str(value.get("source", "unknown")),
                    category=(
                        str(value["category"]).strip()
                        if value.get("category") is not None
                        else None
                    ),
                )
            )
        return records

    def forget(self, identifier: str) -> bool:
        key = identifier.strip()
        if not key:
            return False
        existing = self._store.batch([GetOp(namespace=_LEARNINGS_NS, key=key)])[0]
        if existing is None:
            return False
        self._store.batch([PutOp(namespace=_LEARNINGS_NS, key=key, value=None)])
        return True


def _summarize_learnings(records: list[LearningRecord]) -> str:
    """Fold overflow learnings into one dense summary via the hub's LLM.

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


def format_learning_list(records: list[LearningRecord]) -> str:
    if not records:
        return "No hub learnings have been stored yet."

    lines = ["Stored hub learnings:"]
    for record in records:
        detail = (
            f"{record.identifier} | "
            f"{record.created_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} "
            f"| source={record.source}"
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
    """Render stored learnings for injection into the orchestrator system prompt.

    Most-recent learnings are prioritized and the output is capped so a growing
    learning store cannot unboundedly inflate every LLM call.
    """
    if not records:
        return ""

    ordered = sorted(records, key=lambda r: r.created_at, reverse=True)
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
