"""Explicit runtime-policy validation for callable specialist agents."""

from __future__ import annotations

import shlex
from typing import Any, Mapping

SUPPORTED_RUNTIME_MODES = {"subprocess", "factory_brain"}


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
