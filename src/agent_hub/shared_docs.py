"""Federated search helpers for shared docs across Hub and Agent Factory."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool as lc_tool
from langgraph.store.base import SearchOp

from .config import AGENT_FACTORY_KNOWLEDGE_DB
from .knowledge_store import SqliteStore, get_knowledge_store

_LOCAL_LABEL = "hub"
_FACTORY_LABEL = "agent-factory"


def make_shared_docs_tool() -> Any:
    @lc_tool(
        "search_shared_docs",
        description=(
            "Search shared documentation and trusted-source memory across Agent Hub and Agent "
            "Factory. Use this before answering architecture or routing questions."
        ),
    )
    def _search_shared_docs(query: str) -> str:
        if not query.strip():
            return "Query must not be empty."

        results = []
        results.extend(_search_store(get_knowledge_store(), _LOCAL_LABEL, query))

        factory_db = AGENT_FACTORY_KNOWLEDGE_DB
        if factory_db.exists():
            factory_store = SqliteStore(factory_db)
            results.extend(_search_store(factory_store, _FACTORY_LABEL, query))

        if not results:
            return f"No shared-doc matches found for: {query}"

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
