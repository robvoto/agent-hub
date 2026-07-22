"""Persistent task-run lifecycle store for hub orchestration."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from .config import TASK_RUN_DB
from .log_config import get_human_logger

logger = logging.getLogger(__name__)
human_logger = get_human_logger()

DEFAULT_PROJECT_KEY = "__default__"
"""Stable key for a run with no recorded target_project — either created
before per-project tracking existed, or dispatched with no /project
selected. Both cases mean the same thing: 'whatever the specialist
defaults to on its own', so they must serialize/match against each other."""


def _project_key_of(run: "TaskRun") -> str:
    return run.context.get("target_project") or DEFAULT_PROJECT_KEY


TASK_STATE_RECEIVED = "received"
TASK_STATE_ROUTED = "routed"
TASK_STATE_DISPATCHED = "dispatched"
TASK_STATE_IN_PROGRESS = "in_progress"
TASK_STATE_WAITING_CLARIFICATION = "waiting_clarification"
TASK_STATE_WAITING_APPROVAL = "waiting_approval"
TASK_STATE_SUCCEEDED = "succeeded"
TASK_STATE_FAILED = "failed"
TASK_STATE_CANCELLED = "cancelled"

TASK_STATES = {
    TASK_STATE_RECEIVED,
    TASK_STATE_ROUTED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_WAITING_CLARIFICATION,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELLED,
}

_PAUSED_STATES = {
    TASK_STATE_WAITING_CLARIFICATION,
    TASK_STATE_WAITING_APPROVAL,
}
_TERMINAL_STATES = {
    TASK_STATE_SUCCEEDED,
    TASK_STATE_FAILED,
    TASK_STATE_CANCELLED,
}
_ACTIVE_STATES = {
    TASK_STATE_RECEIVED,
    TASK_STATE_ROUTED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_IN_PROGRESS,
}
_ALLOWED_TRANSITIONS = {
    TASK_STATE_RECEIVED: {
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_WAITING_CLARIFICATION,
        TASK_STATE_WAITING_APPROVAL,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_ROUTED: {
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_WAITING_CLARIFICATION,
        TASK_STATE_WAITING_APPROVAL,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_DISPATCHED: {
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_WAITING_CLARIFICATION,
        TASK_STATE_WAITING_APPROVAL,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_IN_PROGRESS: {
        TASK_STATE_WAITING_CLARIFICATION,
        TASK_STATE_WAITING_APPROVAL,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_WAITING_CLARIFICATION: {
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_WAITING_APPROVAL: {
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED,
        TASK_STATE_CANCELLED,
    },
    TASK_STATE_SUCCEEDED: set(),
    TASK_STATE_FAILED: set(),
    TASK_STATE_CANCELLED: set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    state TEXT NOT NULL,
    selected_agent_id TEXT,
    dispatched_task TEXT,
    final_response TEXT,
    approval_token TEXT,
    error_message TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    raw_result_json TEXT,
    requested_model TEXT,
    effective_model TEXT,
    duration_ms INTEGER,
    usage_json TEXT,
    cost_json TEXT,
    cancellation_reason TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS task_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_run_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT,
    selected_agent_id TEXT,
    approval_token TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_run_id) REFERENCES task_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_task_runs_session_id ON task_runs (session_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_state ON task_runs (state);
CREATE INDEX IF NOT EXISTS idx_task_run_events_task_run_id ON task_run_events (task_run_id);
"""
_TASK_RUN_MIGRATION_COLUMNS = {
    "context_json": "TEXT NOT NULL DEFAULT '{}'",
    "raw_result_json": "TEXT",
    "requested_model": "TEXT",
    "effective_model": "TEXT",
    "duration_ms": "INTEGER",
    "usage_json": "TEXT",
    "cost_json": "TEXT",
    "cancellation_reason": "TEXT",
    "cancelled_at": "TEXT",
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


@dataclass(frozen=True)
class TaskRun:
    id: str
    session_id: str
    user_message: str
    state: str
    selected_agent_id: str | None
    dispatched_task: str | None
    final_response: str | None
    approval_token: str | None
    error_message: str | None
    context: dict
    raw_result: dict | None
    requested_model: str | None
    effective_model: str | None
    duration_ms: int | None
    usage: dict | None
    cost: dict | None
    cancellation_reason: str | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class TaskRunEvent:
    id: int
    task_run_id: str
    from_state: str | None
    to_state: str
    detail: str | None
    selected_agent_id: str | None
    approval_token: str | None
    created_at: datetime


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _ensure_task_run_columns(conn)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_task_run(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        session_id=row["session_id"],
        user_message=row["user_message"],
        state=row["state"],
        selected_agent_id=row["selected_agent_id"],
        dispatched_task=row["dispatched_task"],
        final_response=row["final_response"],
        approval_token=row["approval_token"],
        error_message=row["error_message"],
        context=_json_dict(row["context_json"], default={}),
        raw_result=_json_dict(row["raw_result_json"]),
        requested_model=row["requested_model"],
        effective_model=row["effective_model"],
        duration_ms=row["duration_ms"],
        usage=_json_dict(row["usage_json"]),
        cost=_json_dict(row["cost_json"]),
        cancellation_reason=row["cancellation_reason"],
        cancelled_at=datetime.fromisoformat(row["cancelled_at"]) if row["cancelled_at"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
    )


def _row_to_task_run_event(row: sqlite3.Row) -> TaskRunEvent:
    return TaskRunEvent(
        id=row["id"],
        task_run_id=row["task_run_id"],
        from_state=row["from_state"],
        to_state=row["to_state"],
        detail=row["detail"],
        selected_agent_id=row["selected_agent_id"],
        approval_token=row["approval_token"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class TaskRunStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or TASK_RUN_DB

    def create_run(self, session_id: str, user_message: str) -> TaskRun:
        run_id = str(uuid.uuid4())
        now = _utcnow()
        human_logger.info(
            "Task %s: human asked: %s", run_id[:8], _truncate(user_message)
        )
        with _connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO task_runs (
                       id, session_id, user_message, state, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, session_id, user_message, TASK_STATE_RECEIVED, now, now),
            )
            self._insert_event(
                conn,
                task_run_id=run_id,
                from_state=None,
                to_state=TASK_STATE_RECEIVED,
                detail="Task received by hub orchestrator.",
            )
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        return _row_to_task_run(row)

    def get_run(self, run_id: str) -> TaskRun | None:
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_task_run(row) if row else None

    def list_runs(self, session_id: str | None = None) -> list[TaskRun]:
        query = "SELECT * FROM task_runs"
        params: tuple[str, ...] = ()
        if session_id is not None:
            query += " WHERE session_id=?"
            params = (session_id,)
        query += " ORDER BY created_at ASC"
        with _connect(self._db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_task_run(row) for row in rows]

    def get_latest_paused_run(
        self, session_id: str, *, project_key: str | None = None
    ) -> TaskRun | None:
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM task_runs
                   WHERE session_id=? AND state IN (?, ?)
                   ORDER BY updated_at DESC""",
                (session_id, TASK_STATE_WAITING_CLARIFICATION, TASK_STATE_WAITING_APPROVAL),
            ).fetchall()
        for row in rows:
            run = _row_to_task_run(row)
            if project_key is None or _project_key_of(run) == project_key:
                return run
        return None

    def get_active_or_paused_run_for_project(
        self, project_key: str, *, exclude_run_id: str | None = None
    ) -> TaskRun | None:
        """Any non-terminal run (any session) already targeting this project.

        Used to enforce one in-flight specialist task per project — two
        subprocess dispatches racing on the same repo is a real hazard,
        not just a UX nuisance.
        """
        states = tuple(_ACTIVE_STATES | _PAUSED_STATES)
        placeholders = ",".join("?" for _ in states)
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                f"""SELECT * FROM task_runs
                    WHERE state IN ({placeholders})
                    ORDER BY updated_at DESC""",
                states,
            ).fetchall()
        for row in rows:
            run = _row_to_task_run(row)
            if run.id == exclude_run_id:
                continue
            if _project_key_of(run) == project_key:
                return run
        return None

    def get_latest_active_or_paused_run(
        self, session_id: str, *, project_key: str | None = None
    ) -> TaskRun | None:
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT * FROM task_runs
                   WHERE session_id=? AND state IN (?, ?, ?, ?, ?, ?)
                   ORDER BY updated_at DESC""",
                (
                    session_id,
                    TASK_STATE_RECEIVED,
                    TASK_STATE_ROUTED,
                    TASK_STATE_DISPATCHED,
                    TASK_STATE_IN_PROGRESS,
                    TASK_STATE_WAITING_CLARIFICATION,
                    TASK_STATE_WAITING_APPROVAL,
                ),
            ).fetchall()
        if project_key is None:
            return _row_to_task_run(rows[0]) if rows else None
        for row in rows:
            run = _row_to_task_run(row)
            if _project_key_of(run) == project_key:
                return run
        return None

    def get_latest_completed_or_failed_run(self, session_id: str) -> TaskRun | None:
        with _connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT * FROM task_runs
                   WHERE session_id=? AND state IN (?, ?)
                   ORDER BY finished_at DESC, updated_at DESC LIMIT 1""",
                (session_id, TASK_STATE_SUCCEEDED, TASK_STATE_FAILED),
            ).fetchone()
        return _row_to_task_run(row) if row else None

    def list_events(self, run_id: str) -> list[TaskRunEvent]:
        with _connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM task_run_events WHERE task_run_id=? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
        return [_row_to_task_run_event(row) for row in rows]

    def transition(
        self,
        run_id: str,
        to_state: str,
        *,
        detail: str | None = None,
        selected_agent_id: str | None = None,
        dispatched_task: str | None = None,
        final_response: str | None = None,
        approval_token: str | None = None,
        error_message: str | None = None,
        context_updates: dict | None = None,
        raw_result: dict | None = None,
        requested_model: str | None = None,
        effective_model: str | None = None,
        duration_ms: int | None = None,
        usage: dict | None = None,
        cost: dict | None = None,
        cancellation_reason: str | None = None,
    ) -> TaskRun:
        if to_state not in TASK_STATES:
            raise ValueError(f"Unknown task state: {to_state}")

        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task run: {run_id}")

            current = _row_to_task_run(row)
            allowed = _ALLOWED_TRANSITIONS[current.state]
            if to_state not in allowed:
                raise ValueError(
                    f"Invalid task transition: {current.state} -> {to_state}"
                )

            now = _utcnow()
            finished_at = now if to_state in _TERMINAL_STATES else current.finished_at
            cancelled_at = now if to_state == TASK_STATE_CANCELLED else current.cancelled_at
            merged_context = dict(current.context)
            if context_updates:
                merged_context.update(context_updates)
            conn.execute(
                """UPDATE task_runs
                   SET state=?,
                       selected_agent_id=?,
                       dispatched_task=?,
                       final_response=?,
                       approval_token=?,
                       error_message=?,
                       context_json=?,
                       raw_result_json=?,
                       requested_model=?,
                       effective_model=?,
                       duration_ms=?,
                       usage_json=?,
                       cost_json=?,
                       cancellation_reason=?,
                       cancelled_at=?,
                       updated_at=?,
                       finished_at=?
                   WHERE id=?""",
                (
                    to_state,
                    selected_agent_id or current.selected_agent_id,
                    dispatched_task or current.dispatched_task,
                    final_response or current.final_response,
                    approval_token or current.approval_token,
                    error_message or current.error_message,
                    json.dumps(merged_context, sort_keys=True),
                    _merge_json_value(raw_result, current.raw_result),
                    requested_model or current.requested_model,
                    effective_model or current.effective_model,
                    duration_ms if duration_ms is not None else current.duration_ms,
                    _merge_json_value(usage, current.usage),
                    _merge_json_value(cost, current.cost),
                    (
                        cancellation_reason
                        if cancellation_reason is not None
                        else current.cancellation_reason
                    ),
                    cancelled_at,
                    now,
                    finished_at,
                    run_id,
                ),
            )
            self._insert_event(
                conn,
                task_run_id=run_id,
                from_state=current.state,
                to_state=to_state,
                detail=detail,
                selected_agent_id=selected_agent_id or current.selected_agent_id,
                approval_token=approval_token or current.approval_token,
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert updated is not None
        result = _row_to_task_run(updated)
        human_logger.info(
            "Task %s: %s -> %s%s",
            run_id[:8],
            current.state,
            to_state,
            f" — {detail}" if detail else "",
        )
        if to_state in _TERMINAL_STATES and result.final_response:
            human_logger.info(
                "Task %s: I responded: %s", run_id[:8], _truncate(result.final_response)
            )
        return result

    def set_final_response(self, run_id: str, final_response: str) -> TaskRun:
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task run: {run_id}")

            now = _utcnow()
            conn.execute(
                "UPDATE task_runs SET final_response=?, updated_at=? WHERE id=?",
                (final_response, now, run_id),
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert updated is not None
        return _row_to_task_run(updated)

    def update_context(self, run_id: str, **updates: object) -> TaskRun:
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task run: {run_id}")

            current = _row_to_task_run(row)
            merged_context = dict(current.context)
            merged_context.update(updates)
            now = _utcnow()
            conn.execute(
                "UPDATE task_runs SET context_json=?, updated_at=? WHERE id=?",
                (json.dumps(merged_context, sort_keys=True), now, run_id),
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert updated is not None
        return _row_to_task_run(updated)

    def update_run(
        self,
        run_id: str,
        *,
        final_response: str | None = None,
        approval_token: str | None = None,
        error_message: str | None = None,
        context_updates: dict | None = None,
        raw_result: dict | None = None,
        requested_model: str | None = None,
        effective_model: str | None = None,
        duration_ms: int | None = None,
        usage: dict | None = None,
        cost: dict | None = None,
    ) -> TaskRun:
        with _connect(self._db_path) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown task run: {run_id}")

            current = _row_to_task_run(row)
            merged_context = dict(current.context)
            if context_updates:
                merged_context.update(context_updates)
            now = _utcnow()
            conn.execute(
                """UPDATE task_runs
                   SET final_response=?,
                       approval_token=?,
                       error_message=?,
                       context_json=?,
                       raw_result_json=?,
                       requested_model=?,
                       effective_model=?,
                       duration_ms=?,
                       usage_json=?,
                       cost_json=?,
                       updated_at=?
                   WHERE id=?""",
                (
                    final_response if final_response is not None else current.final_response,
                    approval_token if approval_token is not None else current.approval_token,
                    error_message if error_message is not None else current.error_message,
                    json.dumps(merged_context, sort_keys=True),
                    _merge_json_value(raw_result, current.raw_result),
                    requested_model or current.requested_model,
                    effective_model or current.effective_model,
                    duration_ms if duration_ms is not None else current.duration_ms,
                    _merge_json_value(usage, current.usage),
                    _merge_json_value(cost, current.cost),
                    now,
                    run_id,
                ),
            )
            updated = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert updated is not None
        return _row_to_task_run(updated)

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        task_run_id: str,
        from_state: str | None,
        to_state: str,
        detail: str | None,
        selected_agent_id: str | None = None,
        approval_token: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO task_run_events (
                   task_run_id, from_state, to_state, detail, selected_agent_id,
                   approval_token, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                task_run_id,
                from_state,
                to_state,
                detail,
                selected_agent_id,
                approval_token,
                _utcnow(),
            ),
        )


_store: TaskRunStore | None = None
_current_task_run_id: ContextVar[str | None] = ContextVar("current_task_run_id", default=None)


def get_task_run_store(db_path: Path | None = None) -> TaskRunStore:
    global _store
    if _store is None:
        _store = TaskRunStore(db_path)
        logger.info("Hub task-run store: %s", _store._db_path)
    return _store


def get_current_task_run_id() -> str | None:
    return _current_task_run_id.get()


@contextmanager
def active_task_run(run_id: str) -> Generator[None, None, None]:
    token = _current_task_run_id.set(run_id)
    try:
        yield
    finally:
        _current_task_run_id.reset(token)


def is_active_state(state: str) -> bool:
    return state in _ACTIVE_STATES


def is_paused_state(state: str) -> bool:
    return state in _PAUSED_STATES


def is_terminal_state(state: str) -> bool:
    return state in _TERMINAL_STATES


def _ensure_task_run_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(task_runs)").fetchall()
    }
    for name, definition in _TASK_RUN_MIGRATION_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")


def _json_dict(raw: str | None, default: dict | None = None) -> dict | None:
    if raw is None:
        return default
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return payload
    return default


def _merge_json_value(value: dict | None, current: dict | None) -> str | None:
    if value is None and current is None:
        return None
    merged = current if value is None else value
    return json.dumps(merged, sort_keys=True)
