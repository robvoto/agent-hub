"""Paths and runtime configuration for the Agent Hub."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def configured_model() -> str:
    """Return the model selected by deployment configuration.

    Model choice is deliberately not embedded in source code. A missing
    value must fail closed so a deployment cannot silently use an unapproved
    or stale model.
    """
    model = os.getenv("HUB_MODEL", "").strip()
    if not model:
        raise ConfigurationError(
            "HUB_MODEL is required; set it to an approved model before starting Agent Hub."
        )
    return model


def configured_reasoning_effort() -> str | None:
    """Return the optional provider-neutral reasoning effort override."""
    value = os.getenv("HUB_REASONING_EFFORT", "").strip().lower()
    if not value:
        return None
    allowed = {"none", "low", "medium", "high"}
    if value not in allowed:
        raise ConfigurationError(
            "HUB_REASONING_EFFORT must be one of: none, low, medium, high."
        )
    return value


def chat_model_kwargs(model: str) -> dict[str, str]:
    """Build shared ChatOpenAI kwargs from explicit runtime configuration."""
    kwargs = {"model": model}
    reasoning_effort = configured_reasoning_effort()
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return kwargs


def configured_handoff_reviewer_model() -> str:
    """Return the configured model for the independent handoff reviewer."""
    return os.getenv("HUB_HANDOFF_REVIEW_MODEL", "").strip() or configured_model()


def configured_handoff_reviewer_reasoning_effort() -> str:
    """Return the reviewer's explicit reasoning-effort setting.

    The reviewer deliberately does not inherit ``HUB_REASONING_EFFORT``;
    its reasoning budget is an independently governed runtime setting.
    """
    value = os.getenv("HUB_HANDOFF_REVIEW_REASONING_EFFORT", "none").strip().lower()
    allowed = {"none", "low", "medium", "high"}
    if value not in allowed:
        raise ConfigurationError(
            "HUB_HANDOFF_REVIEW_REASONING_EFFORT must be one of: none, low, medium, high."
        )
    return value


def handoff_reviewer_model_kwargs(model: str) -> dict[str, str]:
    """Build reviewer-only model kwargs without inheriting Hub turn settings."""
    return {
        "model": model,
        "reasoning_effort": configured_handoff_reviewer_reasoning_effort(),
    }


def configured_handoff_reviewer_timeout_seconds() -> float:
    """Return the bounded reviewer request timeout."""
    value = os.getenv("HUB_HANDOFF_REVIEW_TIMEOUT_SECONDS", "30").strip()
    try:
        timeout = float(value)
    except ValueError as exc:
        raise ConfigurationError(
            "HUB_HANDOFF_REVIEW_TIMEOUT_SECONDS must be a positive number."
        ) from exc
    if timeout <= 0 or timeout > 120:
        raise ConfigurationError(
            "HUB_HANDOFF_REVIEW_TIMEOUT_SECONDS must be greater than 0 and at most 120."
        )
    return timeout


def configured_handoff_reviewer_max_tokens() -> int:
    """Return the bounded reviewer output-token limit."""
    value = os.getenv("HUB_HANDOFF_REVIEW_MAX_TOKENS", "1200").strip()
    try:
        max_tokens = int(value)
    except ValueError as exc:
        raise ConfigurationError(
            "HUB_HANDOFF_REVIEW_MAX_TOKENS must be a positive integer."
        ) from exc
    if max_tokens <= 0 or max_tokens > 4000:
        raise ConfigurationError(
            "HUB_HANDOFF_REVIEW_MAX_TOKENS must be greater than 0 and at most 4000."
        )
    return max_tokens

# Agent Factory — hub reads the agent registry from here.
# Override with AGENT_FACTORY_ROOT env var if agent-factory lives elsewhere.
AGENT_FACTORY_ROOT = Path(
    os.environ.get("AGENT_FACTORY_ROOT", Path.home() / "projects" / "agent-factory")
)
AGENT_REGISTRY_DIR = AGENT_FACTORY_ROOT / "config" / "agents"

# Hub-owned persistence — hub is the control plane; all runtime state lives here.
CHECKPOINT_DB = DATA_DIR / "checkpoints.sqlite3"
KNOWLEDGE_DB = DATA_DIR / "knowledge_store.sqlite3"
TASK_RUN_DB = DATA_DIR / "task_runs.sqlite3"
USAGE_LOG_FILE = DATA_DIR / "llm_usage.json"
MANIFEST_CACHE_FILE = DATA_DIR / "agent_manifest_cache.json"
LLM_COST_CATALOG_FILE = Path(
    os.environ.get(
        "HUB_LLM_COST_CATALOG",
        str(CONFIG_DIR / "llm_costs.json"),
    )
)

TELEGRAM_API_BASE = "https://api.telegram.org"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
