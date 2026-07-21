"""Bounded startup health validation for Agent Hub runtime modes."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Literal

from .config import (
    AGENT_FACTORY_ROOT,
    AGENT_REGISTRY_DIR,
    CHECKPOINT_DB,
    DATA_DIR,
    KNOWLEDGE_DB,
    LLM_COST_CATALOG_FILE,
    TASK_RUN_DB,
)
from .cost_log import load_cost_catalog
from .factory_bridge import build_factory_agent_spec
from .registry import AgentSpec
from .runtime_policy import derive_manifest_command, validate_runtime_config

HealthStatus = Literal["PASS", "WARNING", "FAIL"]

_SUPPORTED_MODES = {"chat", "telegram"}


@dataclass(frozen=True)
class HealthCheckResult:
    name: str
    status: HealthStatus
    detail: str


@dataclass(frozen=True)
class StartupHealthReport:
    mode: str
    checks: tuple[HealthCheckResult, ...]

    @property
    def has_failures(self) -> bool:
        return any(check.status == "FAIL" for check in self.checks)

    def render(self) -> str:
        pass_count = sum(check.status == "PASS" for check in self.checks)
        warning_count = sum(check.status == "WARNING" for check in self.checks)
        fail_count = sum(check.status == "FAIL" for check in self.checks)
        lines = [f"Startup health check ({self.mode})"]
        lines.extend(
            f"{check.status:<7} {check.name}: {check.detail}"
            for check in self.checks
        )
        lines.append(
            f"Summary: {pass_count} PASS, {warning_count} WARNING, {fail_count} FAIL"
        )
        return "\n".join(lines)


def ensure_healthy_startup(
    mode: str,
    *,
    telegram_token: str | None = None,
) -> StartupHealthReport:
    report = run_startup_healthcheck(mode, telegram_token=telegram_token)
    print(report.render())
    if report.has_failures:
        raise SystemExit(1)
    return report


def run_startup_healthcheck(
    mode: str,
    *,
    telegram_token: str | None = None,
) -> StartupHealthReport:
    if mode not in _SUPPORTED_MODES:
        raise ValueError(f"Unsupported startup mode: {mode!r}")

    checks: list[HealthCheckResult] = [
        _check_openai_api_key(),
    ]
    if mode == "telegram":
        checks.extend(_check_telegram_config(telegram_token=telegram_token))
    checks.extend(_check_agent_factory_layout())
    checks.extend(_check_agent_specs())
    checks.append(_check_data_dir())
    checks.extend(_check_sqlite_paths())
    checks.append(_check_cost_catalog())
    return StartupHealthReport(mode=mode, checks=tuple(checks))


def _check_openai_api_key() -> HealthCheckResult:
    token = os.getenv("OPENAI_API_KEY", "").strip()
    if not token:
        return HealthCheckResult(
            name="OPENAI_API_KEY",
            status="FAIL",
            detail="Environment variable is not set.",
        )
    return HealthCheckResult(
        name="OPENAI_API_KEY",
        status="PASS",
        detail="Environment variable is set.",
    )


def _check_telegram_config(*, telegram_token: str | None) -> list[HealthCheckResult]:
    checks: list[HealthCheckResult] = []
    token = (telegram_token or os.getenv("HUB_BOT_TOKEN", "")).strip()
    if not token:
        checks.append(
            HealthCheckResult(
                name="HUB_BOT_TOKEN",
                status="FAIL",
                detail="Telegram mode requires a bot token.",
            )
        )
    else:
        checks.append(
            HealthCheckResult(
                name="HUB_BOT_TOKEN",
                status="PASS",
                detail="Telegram bot token is configured.",
            )
        )

    raw_chat_ids = os.getenv("HUB_ALLOWED_CHAT_IDS", "").strip()
    if not raw_chat_ids:
        checks.append(
            HealthCheckResult(
                name="HUB_ALLOWED_CHAT_IDS",
                status="WARNING",
                detail="No chat allowlist is configured; any Telegram chat may talk to the bot.",
            )
        )
        return checks

    try:
        parsed = _parse_allowed_chat_ids(raw_chat_ids)
    except ValueError as exc:
        checks.append(
            HealthCheckResult(
                name="HUB_ALLOWED_CHAT_IDS",
                status="FAIL",
                detail=str(exc),
            )
        )
    else:
        checks.append(
            HealthCheckResult(
                name="HUB_ALLOWED_CHAT_IDS",
                status="PASS",
                detail=f"Parsed {len(parsed)} allowed Telegram chat ID(s).",
            )
        )
    return checks


def _parse_allowed_chat_ids(raw_value: str) -> set[int]:
    try:
        parsed = {int(item.strip()) for item in raw_value.split(",") if item.strip()}
    except ValueError as exc:
        raise ValueError("Chat allowlist must be a comma-separated list of integers.") from exc
    if not parsed:
        raise ValueError("Chat allowlist is empty after parsing.")
    return parsed


def _check_agent_factory_layout() -> list[HealthCheckResult]:
    checks: list[HealthCheckResult] = []
    root = AGENT_FACTORY_ROOT.expanduser()
    if not root.exists():
        checks.append(
            HealthCheckResult(
                name="AGENT_FACTORY_ROOT",
                status="FAIL",
                detail=f"Project path does not exist: {root}",
            )
        )
    elif not root.is_dir():
        checks.append(
            HealthCheckResult(
                name="AGENT_FACTORY_ROOT",
                status="FAIL",
                detail=f"Project path is not a directory: {root}",
            )
        )
    else:
        checks.append(
            HealthCheckResult(
                name="AGENT_FACTORY_ROOT",
                status="PASS",
                detail=f"Found Agent Factory project path at {root}.",
            )
        )

    registry_dir = AGENT_REGISTRY_DIR.expanduser()
    if not registry_dir.exists():
        checks.append(
            HealthCheckResult(
                name="AGENT_REGISTRY_DIR",
                status="FAIL",
                detail=f"Registry directory does not exist: {registry_dir}",
            )
        )
    elif not registry_dir.is_dir():
        checks.append(
            HealthCheckResult(
                name="AGENT_REGISTRY_DIR",
                status="FAIL",
                detail=f"Registry path is not a directory: {registry_dir}",
            )
        )
    else:
        checks.append(
            HealthCheckResult(
                name="AGENT_REGISTRY_DIR",
                status="PASS",
                detail=f"Found registry directory at {registry_dir}.",
            )
        )
    return checks


def _check_agent_specs() -> list[HealthCheckResult]:
    registry_dir = AGENT_REGISTRY_DIR.expanduser()
    if not registry_dir.is_dir():
        return []

    checks: list[HealthCheckResult] = []
    spec_files = sorted(registry_dir.glob("*/agent.json"))
    if not spec_files:
        return [
            HealthCheckResult(
                name="Agent specs",
                status="FAIL",
                detail=f"No enabled agent.json files found in {registry_dir}.",
            )
        ]

    loaded_specs: list[AgentSpec] = []
    for spec_file in spec_files:
        checks.append(_check_agent_spec_file(spec_file, loaded_specs))

    if not any(spec.id == "agent-factory" for spec in loaded_specs):
        factory_spec = build_factory_agent_spec(AGENT_FACTORY_ROOT)
        if factory_spec is None:
            checks.append(
                HealthCheckResult(
                    name="system agent: agent-factory",
                    status="FAIL",
                    detail="Agent Factory bridge spec could not be built from the configured root.",
                )
            )
        else:
            checks.append(_validate_spec(factory_spec, source="system bridge"))

    return checks


def _check_agent_spec_file(spec_file: Path, loaded_specs: list[AgentSpec]) -> HealthCheckResult:
    try:
        data = json.loads(spec_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("agent.json must contain a JSON object.")
        spec = AgentSpec(
            id=data["id"],
            name=data.get("name", data["id"]),
            purpose=data.get("purpose", ""),
            aliases=data.get("aliases", []),
            tools=data.get("tools", []),
            version=data.get("version", "1.0.0"),
            backlog_sheet_id=data.get("backlog_sheet_id"),
            runtime=data.get("runtime", {}),
        )
    except Exception as exc:
        return HealthCheckResult(
            name=f"agent spec: {spec_file.parent.name}",
            status="FAIL",
            detail=f"Could not parse {spec_file}: {exc}",
        )

    loaded_specs.append(spec)
    return _validate_spec(spec, source=str(spec_file))


def _validate_spec(spec: AgentSpec, *, source: str) -> HealthCheckResult:
    try:
        validate_runtime_config(spec.id, spec.runtime)
        working_directory = _resolve_working_directory(spec.runtime["working_directory"])
        if not working_directory.exists():
            raise ValueError(f"Working directory does not exist: {working_directory}")
        if not working_directory.is_dir():
            raise ValueError(f"Working directory is not a directory: {working_directory}")

        if spec.runtime["mode"] == "subprocess":
            _require_command(
                spec.runtime["entrypoint"],
                working_directory=working_directory,
                label="entrypoint",
            )

        manifest_command = derive_manifest_command(spec.runtime)
        if not manifest_command:
            raise ValueError("Manifest command is missing and could not be derived.")
        _require_command(
            manifest_command,
            working_directory=working_directory,
            label="manifest command",
        )
    except Exception as exc:
        return HealthCheckResult(
            name=f"agent spec: {spec.id}",
            status="FAIL",
            detail=f"{exc} (source: {source})",
        )

    return HealthCheckResult(
        name=f"agent spec: {spec.id}",
        status="PASS",
        detail=(
            f"Validated runtime.mode={spec.runtime['mode']}, working directory, "
            f"and command configuration (source: {source})."
        ),
    )


def _resolve_working_directory(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path)


def _require_command(
    command: str,
    *,
    working_directory: Path,
    label: str,
) -> Path:
    tokens = shlex.split(command)
    if not tokens:
        raise ValueError(f"{label.capitalize()} is empty.")

    executable = tokens[0]
    if "/" in executable:
        candidate = Path(executable).expanduser()
        resolved = candidate if candidate.is_absolute() else working_directory / candidate
        if resolved.exists():
            return resolved
        raise ValueError(f"{label.capitalize()} executable does not exist: {resolved}")

    resolved_name = which(executable)
    if resolved_name:
        return Path(resolved_name)
    raise ValueError(f"{label.capitalize()} executable is not on PATH: {executable}")


def _check_data_dir() -> HealthCheckResult:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return HealthCheckResult(
            name="DATA_DIR",
            status="FAIL",
            detail=f"Could not create or access hub data directory {DATA_DIR}: {exc}",
        )
    return HealthCheckResult(
        name="DATA_DIR",
        status="PASS",
        detail=f"Hub data directory is writable at {DATA_DIR}.",
    )


def _check_sqlite_paths() -> list[HealthCheckResult]:
    return [
        _probe_sqlite_path("CHECKPOINT_DB", CHECKPOINT_DB),
        _probe_sqlite_path("KNOWLEDGE_DB", KNOWLEDGE_DB),
        _probe_sqlite_path("TASK_RUN_DB", TASK_RUN_DB),
    ]


def _probe_sqlite_path(name: str, path: Path) -> HealthCheckResult:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _startup_health_probe (id INTEGER)")
            conn.execute("DROP TABLE _startup_health_probe")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        return HealthCheckResult(
            name=name,
            status="FAIL",
            detail=f"SQLite write access failed for {path}: {exc}",
        )
    return HealthCheckResult(
        name=name,
        status="PASS",
        detail=f"SQLite write access verified for {path}.",
    )


def _check_cost_catalog() -> HealthCheckResult:
    try:
        if not LLM_COST_CATALOG_FILE.exists():
            raise FileNotFoundError(f"Cost catalog file does not exist: {LLM_COST_CATALOG_FILE}")
        catalog = load_cost_catalog(LLM_COST_CATALOG_FILE)
        models = catalog.get("models", {})
        if not isinstance(models, dict):
            raise ValueError("Cost catalog models entry must be a JSON object.")
    except Exception as exc:
        return HealthCheckResult(
            name="LLM_COST_CATALOG",
            status="FAIL",
            detail=str(exc),
        )
    return HealthCheckResult(
        name="LLM_COST_CATALOG",
        status="PASS",
        detail=f"Loaded cost catalog from {LLM_COST_CATALOG_FILE}.",
    )
