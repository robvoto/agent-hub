"""Paths and constants for the Agent Hub."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

# Agent Factory — hub reads the agent registry from here.
# Override with AGENT_FACTORY_ROOT env var if agent-factory lives elsewhere.
AGENT_FACTORY_ROOT = Path(
    os.environ.get("AGENT_FACTORY_ROOT", Path.home() / "projects" / "agent-factory")
)
AGENT_REGISTRY_DIR = AGENT_FACTORY_ROOT / "config" / "agents"

# Hub-owned persistence — hub is the control plane; all runtime state lives here.
CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite3"
KNOWLEDGE_DB = DATA_DIR / "knowledge_store.sqlite3"

DEFAULT_MODEL = "gpt-4.1-mini"
TELEGRAM_API_BASE = "https://api.telegram.org"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
