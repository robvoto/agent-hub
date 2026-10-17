"""Explicit runtime-policy validation for callable specialist agents."""

from __future__ import annotations

import os
import re
import shlex
from typing import Any, Mapping

SUPPORTED_RUNTIME_MODES = {"subprocess", "factory_brain"}
_RESERVED_STAGED_ENV_NAMES = {"HUB_BOT_TOKEN", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN"}


def validate_runtime_config(agent_id: str, runtime: Mapping[str, Any]) -> None:
    mode = runtime.get("mode")
    if mode not in SUPPORTED_RUNTIME_MODES:
        raise ValueError(
            f"Agent '{agent_id}' has unsupported runtime.mode {mode!r}. "
            f"Supported modes: {sorted(SUPPORTED_RUNTIME_MODES)}."
        )

    if mode == "subprocess":
        _require_fields(
            agent_id,
            runtime,
            "entrypoint",
            "working_directory",
            "input_arg",
            "output_arg",
            "default_execution_mode",
        )
        execution_mode = runtime.get("default_execution_mode")
        if execution_mode not in {"instruction_only", "execute"}:
            raise ValueError(
                f"Agent '{agent_id}' has invalid default_execution_mode {execution_mode!r}. "
                "Expected 'instruction_only' or 'execute'."
            )
        allowed_environment = runtime.get("environment_allowlist", [])
        if not isinstance(allowed_environment, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            for name in allowed_environment
        ):
            raise ValueError(f"Agent '{agent_id}' has an invalid environment allowlist.")
        if any(
            name in _RESERVED_STAGED_ENV_NAMES
            or name.startswith(("HUB_", "OPENAI_"))
            for name in allowed_environment
        ):
            raise ValueError(
                f"Agent '{agent_id}' requests a Hub-only credential "
                "that staged agents cannot receive."
            )
        return

    if mode == "factory_brain":
        _require_fields(
            agent_id,
            runtime,
            "working_directory",
            "manifest_command",
        )


def derive_manifest_command(runtime: Mapping[str, Any]) -> str | None:
    explicit = runtime.get("manifest_command")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    entrypoint = runtime.get("entrypoint")
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return None

    tokens = shlex.split(entrypoint)
    if tokens and tokens[-1] == "run-agent-task":
        tokens[-1] = "manifest"
        return shlex.join(tokens)
    return None


def _require_fields(agent_id: str, runtime: Mapping[str, Any], *fields: str) -> None:
    missing = [
        field
        for field in fields
        if not isinstance(runtime.get(field), str) or not str(runtime.get(field)).strip()
    ]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"Agent '{agent_id}' is missing required runtime fields: {joined}.")


def staged_subprocess_environment(
    runtime: Mapping[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a minimal child environment for staged subprocesses."""
    source = os.environ if environ is None else environ
    names = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "PATHEXT"}
    names.update(runtime.get("environment_allowlist", []))
    return {name: source[name] for name in names if name in source}
