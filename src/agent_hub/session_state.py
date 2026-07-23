"""Persists the Hub's current session id across process restarts.

HubOrchestrator's session_id keys everything durable: LangGraph thread_id in
checkpoints.sqlite3, task-run history in task_runs.sqlite3 (used by /last and
/status), and the per-session /project and /learn-mode selections. Those
stores already survive a restart; without persisting the session_id pointer
itself, a fresh random uuid4 on every start orphaned all of it. See
AGENT-HUB-032.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.store.base import GetOp, PutOp

from .knowledge_store import get_knowledge_store

_NAMESPACE = ("hub", "session_state")
_KEY = "current"


def load_or_create_session_id(store: Any = None) -> str:
    """Return the persisted current session id, creating one on first-ever run."""
    store = store or get_knowledge_store()
    item = store.batch([GetOp(namespace=_NAMESPACE, key=_KEY)])[0]
    if item is not None:
        session_id = item.value.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    session_id = str(uuid.uuid4())
    _persist(store, session_id)
    return session_id


def persist_session_id(session_id: str, store: Any = None) -> None:
    """Write a new current session id (used by /new and /reset)."""
    _persist(store or get_knowledge_store(), session_id)


def _persist(store: Any, session_id: str) -> None:
    existing = store.batch([GetOp(namespace=_NAMESPACE, key=_KEY)])[0]
    epoch = int(existing.value.get("epoch", 0)) + 1 if existing is not None else 1
    store.batch(
        [PutOp(namespace=_NAMESPACE, key=_KEY, value={"session_id": session_id, "epoch": epoch})]
    )
