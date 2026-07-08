"""Tests for the SqliteStore knowledge store."""

import pytest
from langgraph.store.base import GetOp, PutOp, SearchOp

from agent_hub.knowledge_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    return SqliteStore(tmp_path / "test_store.sqlite3")


def test_put_and_get(store):
    ns = ("hub", "learnings")
    store.batch([PutOp(namespace=ns, key="fact1", value={"text": "hello"})])
    results = store.batch([GetOp(namespace=ns, key="fact1")])
    item = results[0]
    assert item is not None
    assert item.value == {"text": "hello"}


def test_get_missing_key(store):
    ns = ("hub", "learnings")
    results = store.batch([GetOp(namespace=ns, key="nonexistent")])
    assert results[0] is None


def test_put_delete(store):
    ns = ("hub", "learnings")
    store.batch([PutOp(namespace=ns, key="to_delete", value={"x": 1})])
    store.batch([PutOp(namespace=ns, key="to_delete", value=None)])
    results = store.batch([GetOp(namespace=ns, key="to_delete")])
    assert results[0] is None


def test_search_by_namespace(store):
    store.batch([
        PutOp(namespace=("hub", "learnings"), key="k1", value={"text": "apple"}),
        PutOp(namespace=("hub", "learnings"), key="k2", value={"text": "banana"}),
        PutOp(namespace=("shared", "docs"), key="k3", value={"text": "cherry"}),
    ])
    results = store.batch([SearchOp(namespace_prefix=("hub", "learnings"), limit=10, offset=0)])
    keys = {r.key for r in results[0]}
    assert keys == {"k1", "k2"}


def test_search_with_query(store):
    store.batch([
        PutOp(namespace=("hub", "learnings"), key="a", value={"text": "python rocks"}),
        PutOp(namespace=("hub", "learnings"), key="b", value={"text": "java is okay"}),
    ])
    results = store.batch([SearchOp(namespace_prefix=("hub", "learnings"), query="python", limit=10, offset=0)])
    items = results[0]
    assert len(items) == 1
    assert items[0].key == "a"


def test_put_upsert(store):
    ns = ("hub", "learnings")
    store.batch([PutOp(namespace=ns, key="key1", value={"v": 1})])
    store.batch([PutOp(namespace=ns, key="key1", value={"v": 2})])
    results = store.batch([GetOp(namespace=ns, key="key1")])
    assert results[0].value == {"v": 2}
