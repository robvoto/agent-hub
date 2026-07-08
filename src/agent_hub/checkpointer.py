"""Persistent SqliteSaver checkpointer singleton for all orchestrator graphs."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from .config import CHECKPOINT_DB, ensure_data_dir

if TYPE_CHECKING:
    from langgraph.checkpoint.sqlite import SqliteSaver

_conn: sqlite3.Connection | None = None
_checkpointer: "SqliteSaver | None" = None


def get_checkpointer() -> "SqliteSaver":
    global _conn, _checkpointer
    if _checkpointer is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        ensure_data_dir()
        _conn = sqlite3.connect(str(CHECKPOINT_DB), check_same_thread=False)
        _checkpointer = SqliteSaver(_conn)
    return _checkpointer
