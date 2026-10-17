"""Paths and runtime configuration for the Agent Hub."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROFILE_FOR_DATA = os.getenv("HUB_PROFILE", "live").strip().lower() or "live"
_LIVE_DATA_DIR = (PROJECT_ROOT / "data").resolve()
_DEFAULT_DATA_DIR = (
    Path.home() / ".local" / "share" / "agent-hub" / "test"
    if _PROFILE_FOR_DATA == "test"
    else _LIVE_DATA_DIR
)
DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", _DEFAULT_DATA_DIR)).expanduser()
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


# Agent Factory — hub reads the agent registry from here.
# Override with AGENT_FACTORY_ROOT env var if agent-factory lives elsewhere.
AGENT_FACTORY_ROOT = Path(
    os.environ.get("AGENT_FACTORY_ROOT", Path.home() / "projects" / "agent-factory")
)
AGENT_REGISTRY_DIR = AGENT_FACTORY_ROOT / "config" / "agents"
AGENT_STAGING_DIR = AGENT_FACTORY_ROOT / "staging" / "agents"


def configured_hub_profile() -> str:
    """Return the isolated Agent Hub profile selected for this process."""
    profile = os.getenv("HUB_PROFILE", "live").strip().lower() or "live"
    if profile not in {"live", "test"}:
        raise ConfigurationError("HUB_PROFILE must be either 'live' or 'test'.")
    return profile


def validate_test_data_dir(data_dir: Path | None = None) -> Path:
    """Ensure test runs cannot share storage with the live Hub profile."""
    selected = (data_dir or DATA_DIR).expanduser().resolve()
    live = _LIVE_DATA_DIR.resolve()
    if selected == live or live in selected.parents or selected in live.parents:
        raise ConfigurationError(
            "Test mode needs its own data folder, separate from the live Hub data folder."
        )
    return selected


def configured_staged_agent_ids() -> tuple[str, ...]:
    """Return the explicit staged-agent allowlist for HUB_PROFILE=test."""
    raw = os.getenv("HUB_STAGED_AGENT_IDS", "")
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))

# Hub-owned persistence — hub is the control plane; all runtime state lives here.
if _PROFILE_FOR_DATA == "test":
    DATA_DIR = validate_test_data_dir(DATA_DIR)
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
