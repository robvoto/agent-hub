"""Persist Hub session ids across process restarts, optionally per chat scope.

HubOrchestrator's session_id keys everything durable: LangGraph thread_id in
checkpoints.sqlite3, task-run history in task_runs.sqlite3 (used by /last and
/status), and the per-session /project and /learn-mode selections. Those
stores already survive a restart; without persisting the session_id pointer
itself, a fresh random uuid4 on every start orphaned all of it. The default
``current`` scope preserves the existing CLI and primary Telegram session;
additional Telegram chats use their own stable scope. See AGENT-HUB-032.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.store.base import GetOp, PutOp

from .knowledge_store import get_knowledge_store

_NAMESPACE = ("hub", "session_state")
_KEY = "current"


def _key_for_scope(scope: str) -> str:
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("Session scope must be a non-empty string.")
    return _KEY if scope == "current" else f"scope:{scope}"


def load_or_create_session_id(store: Any = None, *, scope: str = "current") -> str:
    """Return a persisted session id for this scope, creating it on first use."""
    store = store or get_knowledge_store()
    key = _key_for_scope(scope)
    item = store.batch([GetOp(namespace=_NAMESPACE, key=key)])[0]
    if item is not None:
        session_id = item.value.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    session_id = str(uuid.uuid4())
    _persist(store, session_id, scope=scope)
    return session_id


def persist_session_id(
    session_id: str, store: Any = None, *, scope: str = "current"
) -> None:
    """Write a new session id for this scope (used by /new and /reset)."""
    _persist(store or get_knowledge_store(), session_id, scope=scope)


def _persist(store: Any, session_id: str, *, scope: str = "current") -> None:
    key = _key_for_scope(scope)
    existing = store.batch([GetOp(namespace=_NAMESPACE, key=key)])[0]
    epoch = int(existing.value.get("epoch", 0)) + 1 if existing is not None else 1
    store.batch(
        [PutOp(namespace=_NAMESPACE, key=key, value={"session_id": session_id, "epoch": epoch})]
    )
