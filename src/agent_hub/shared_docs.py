"""Federated search helpers for shared docs across Hub and any opted-in specialist.

A specialist opts in by declaring `knowledge_db` (a path to its own SqliteStore
file) in its agent.json. Hub does not hardcode which specialist repos it reads
from — it searches Hub's own store plus whichever registered specialists have
declared a knowledge_db, the same way any other agent.json field works.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool as lc_tool
from langgraph.store.base import SearchOp

from .knowledge_store import SqliteStore, get_knowledge_store
from .log_config import get_human_logger
from .registry import AgentSpec
from .task_runs import get_current_task_run_id

_LOCAL_LABEL = "hub"
human_logger = get_human_logger()


def _human_task_log(message: str, *args: Any) -> None:
    task_run_id = get_current_task_run_id()
    if task_run_id:
        human_logger.info("Task %s: " + message, task_run_id[:8], *args)
        return
    human_logger.info(message, *args)


def make_shared_docs_tool(registry: list[AgentSpec] | None = None) -> Any:
    specialist_dbs = [
        (spec.id, spec.extensions.get("knowledge_db"))
        for spec in (registry or [])
        if spec.extensions.get("knowledge_db")
    ]

    @lc_tool(
        "search_shared_docs",
        description=(
            "Search shared documentation and trusted-source memory across Agent Hub and any "
            "specialist that has opted its knowledge store in. Use this before answering "
            "architecture or routing questions."
        ),
    )
    def _search_shared_docs(query: str) -> str:
        if not query.strip():
            return "Query must not be empty."

        _human_task_log("Checking shared docs for: %s", query.strip())
        results = []
        results.extend(_search_store(get_knowledge_store(), _LOCAL_LABEL, query))

        for source_label, db_path in specialist_dbs:
            resolved = Path(db_path)
            if resolved.exists():
                results.extend(_search_store(SqliteStore(resolved), source_label, query))

        if not results:
            _human_task_log("Shared docs search found no matches.")
            return f"No shared-doc matches found for: {query}"

        _human_task_log("Shared docs search found %d match(es).", len(results))

        lines = []
        for item in results[:10]:
            lines.append(
                f"[{item['source']}] {item['namespace']} / {item['key']}: "
                f"{_preview_value(item['value'])}"
            )
        return "\n".join(lines)

    return _search_shared_docs


def _search_store(store: SqliteStore, source: str, query: str) -> list[dict[str, Any]]:
    items = []
    for namespace in (("shared", "docs"), ("shared", "trusted")):
        results = store.batch(
            [SearchOp(namespace_prefix=namespace, query=query, limit=5, offset=0)]
        )
        for result in results[0] or []:
            items.append(
                {
                    "source": source,
                    "namespace": "/".join(result.namespace),
                    "key": result.key,
                    "value": result.value,
                }
            )
    return items


def _preview_value(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=True)
    if len(rendered) > 180:
        return rendered[:177] + "..."
    return rendered
