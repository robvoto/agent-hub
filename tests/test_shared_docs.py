from __future__ import annotations

from langgraph.store.base import PutOp

from agent_hub.knowledge_store import SqliteStore, get_knowledge_store
from agent_hub.registry import AgentSpec
from agent_hub.shared_docs import make_shared_docs_tool


def _put(store: SqliteStore, key: str, value: dict) -> None:
    store.batch([PutOp(namespace=("shared", "docs"), key=key, value=value)])


def test_searches_hub_store_with_no_registry():
    _put(get_knowledge_store(), "hub-note", {"content": "restart continuity design notes"})

    tool = make_shared_docs_tool()
    result = tool.invoke({"query": "continuity"})

    assert "[hub]" in result
    assert "hub-note" in result


def test_searches_specialist_store_that_opts_in(tmp_path):
    specialist_db = tmp_path / "specialist_knowledge.sqlite3"
    specialist_store = SqliteStore(specialist_db)
    _put(specialist_store, "specialist-note", {"content": "specialist architecture notes"})

    registry = [
        AgentSpec(
            id="ai-tech-lead",
            name="AI Tech Lead",
            purpose="Implements code changes.",
            runtime={},
            extensions={"knowledge_db": str(specialist_db)},
        )
    ]
    tool = make_shared_docs_tool(registry)
    result = tool.invoke({"query": "architecture"})

    assert "[ai-tech-lead]" in result
    assert "specialist-note" in result


def test_skips_specialist_without_knowledge_db_declared():
    registry = [
        AgentSpec(id="ai-tech-lead", name="AI Tech Lead", purpose="Implements code changes.", runtime={})
    ]
    tool = make_shared_docs_tool(registry)

    result = tool.invoke({"query": "anything"})

    assert result == "No shared-doc matches found for: anything"


def test_skips_specialist_knowledge_db_that_does_not_exist(tmp_path):
    registry = [
        AgentSpec(
            id="ai-tech-lead",
            name="AI Tech Lead",
            purpose="Implements code changes.",
            runtime={},
            extensions={"knowledge_db": str(tmp_path / "does-not-exist.sqlite3")},
        )
    ]
    tool = make_shared_docs_tool(registry)

    result = tool.invoke({"query": "anything"})

    assert result == "No shared-doc matches found for: anything"
