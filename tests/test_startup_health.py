"""Tests for bounded startup health validation."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from agent_hub.registry import AgentSpec
from agent_hub.startup_health import ensure_healthy_startup, run_startup_healthcheck


def _configure_valid_startup(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    import agent_hub.startup_health as startup_health

    root = tmp_path / "agent-factory"
    registry = root / "config" / "agents"
    agent_dir = registry / "ai-tech-lead"
    workdir = tmp_path / "ai-tech-lead"
    data_dir = tmp_path / "data"
    cost_catalog = tmp_path / "config" / "llm_costs.json"

    agent_dir.mkdir(parents=True)
    workdir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    cost_catalog.parent.mkdir(parents=True)

    spec_file = agent_dir / "agent.json"
    spec_file.write_text(
        json.dumps(
            {
                "id": "ai-tech-lead",
                "name": "AI Tech Lead",
                "purpose": "Implements code changes",
                "runtime": {
                    "mode": "subprocess",
                    "entrypoint": "fake-agent run-agent-task",
                    "working_directory": str(workdir),
                    "input_arg": "--input-json",
                    "output_arg": "--output-json",
                    "default_execution_mode": "instruction_only",
                },
            }
        ),
        encoding="utf-8",
    )
    cost_catalog.write_text(
        json.dumps(
            {
                "models": {
                    "gpt-4.1-mini": {
                        "status": "unknown",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(startup_health, "AGENT_FACTORY_ROOT", root)
    monkeypatch.setattr(startup_health, "AGENT_REGISTRY_DIR", registry)
    monkeypatch.setattr(startup_health, "DATA_DIR", data_dir)
    monkeypatch.setattr(startup_health, "CHECKPOINT_DB", data_dir / "checkpoints.sqlite3")
    monkeypatch.setattr(startup_health, "KNOWLEDGE_DB", data_dir / "knowledge_store.sqlite3")
    monkeypatch.setattr(startup_health, "TASK_RUN_DB", data_dir / "task_runs.sqlite3")
    monkeypatch.setattr(startup_health, "LLM_COST_CATALOG_FILE", cost_catalog)
    monkeypatch.setattr(
        startup_health,
        "_require_command",
        lambda command, *, working_directory, label: working_directory / label,
    )
    monkeypatch.setattr(
        startup_health,
        "build_factory_agent_spec",
        lambda root=None: AgentSpec(
            id="agent-factory",
            name="Agent Factory",
            purpose="Stages specialist agents",
            runtime={
                "mode": "factory_brain",
                "working_directory": str(root or startup_health.AGENT_FACTORY_ROOT),
                "manifest_command": "fake-factory-manifest",
            },
        ),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("HUB_BOT_TOKEN", "test-telegram-token")
    monkeypatch.setenv("HUB_ALLOWED_CHAT_IDS", "1001,1002")

    return {
        "root": root,
        "registry": registry,
        "agent_dir": agent_dir,
        "spec_file": spec_file,
        "workdir": workdir,
        "data_dir": data_dir,
        "cost_catalog": cost_catalog,
    }


def _failure_detail(report, name: str) -> str:
    for check in report.checks:
        if check.name == name:
            return check.detail
    raise AssertionError(f"Missing health check: {name}")


def _write_invalid_spec(_monkeypatch, ctx, _module) -> None:
    ctx["spec_file"].write_text("not-json", encoding="utf-8")


def _write_missing_manifest_spec(_monkeypatch, ctx, _module) -> None:
    ctx["spec_file"].write_text(
        json.dumps(
            {
                "id": "ai-tech-lead",
                "name": "AI Tech Lead",
                "purpose": "Implements code changes",
                "runtime": {
                    "mode": "subprocess",
                    "entrypoint": "fake-agent",
                    "working_directory": str(ctx["workdir"]),
                    "input_arg": "--input-json",
                    "output_arg": "--output-json",
                    "default_execution_mode": "instruction_only",
                },
            }
        ),
        encoding="utf-8",
    )


def _write_invalid_cost_catalog(_monkeypatch, ctx, _module) -> None:
    ctx["cost_catalog"].write_text("{", encoding="utf-8")


def test_chat_healthcheck_passes_for_valid_configuration(monkeypatch, tmp_path):
    _configure_valid_startup(monkeypatch, tmp_path)

    report = run_startup_healthcheck("chat")

    assert not report.has_failures
    assert "Summary:" in report.render()
    assert "Startup check looks good for chat." in report.render_human()
    assert all(check.status != "FAIL" for check in report.checks)


def test_telegram_healthcheck_warns_without_allowlist(monkeypatch, tmp_path):
    _configure_valid_startup(monkeypatch, tmp_path)
    monkeypatch.delenv("HUB_ALLOWED_CHAT_IDS", raising=False)

    report = run_startup_healthcheck("telegram", telegram_token="provided-token")

    assert not report.has_failures
    assert _failure_detail(report, "HUB_ALLOWED_CHAT_IDS").startswith(
        "No chat allowlist is configured"
    )
    warning = next(check for check in report.checks if check.name == "HUB_ALLOWED_CHAT_IDS")
    assert warning.status == "WARNING"


@pytest.mark.parametrize(
    ("mode", "mutate", "expected_name", "expected_text"),
    [
        (
            "chat",
            lambda monkeypatch, _ctx, _module: monkeypatch.delenv("OPENAI_API_KEY", raising=False),
            "OPENAI_API_KEY",
            "not set",
        ),
        (
            "telegram",
            lambda monkeypatch, _ctx, _module: monkeypatch.delenv("HUB_BOT_TOKEN", raising=False),
            "HUB_BOT_TOKEN",
            "requires a bot token",
        ),
        (
            "telegram",
            lambda monkeypatch, _ctx, _module: monkeypatch.setenv("HUB_ALLOWED_CHAT_IDS", "abc"),
            "HUB_ALLOWED_CHAT_IDS",
            "comma-separated list of integers",
        ),
        (
            "chat",
            lambda _monkeypatch, ctx, _module: shutil.rmtree(ctx["root"]),
            "AGENT_FACTORY_ROOT",
            "does not exist",
        ),
        (
            "chat",
            lambda _monkeypatch, ctx, _module: shutil.rmtree(ctx["registry"]),
            "AGENT_REGISTRY_DIR",
            "does not exist",
        ),
        (
            "chat",
            _write_invalid_spec,
            "agent spec: ai-tech-lead",
            "Could not parse",
        ),
        (
            "chat",
            lambda _monkeypatch, ctx, _module: shutil.rmtree(ctx["workdir"]),
            "agent spec: ai-tech-lead",
            "Working directory does not exist",
        ),
        (
            "chat",
            _write_missing_manifest_spec,
            "agent spec: ai-tech-lead",
            "Manifest command is missing",
        ),
        (
            "chat",
            lambda monkeypatch, _ctx, module: monkeypatch.setattr(
                module.sqlite3,
                "connect",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("denied")),
            ),
            "CHECKPOINT_DB",
            "SQLite write access failed",
        ),
        (
            "chat",
            _write_invalid_cost_catalog,
            "LLM_COST_CATALOG",
            "Expecting property name enclosed in double quotes",
        ),
    ],
)
def test_startup_healthcheck_reports_critical_failures(
    monkeypatch,
    tmp_path,
    mode,
    mutate,
    expected_name,
    expected_text,
):
    import agent_hub.startup_health as startup_health

    ctx = _configure_valid_startup(monkeypatch, tmp_path)
    mutate(monkeypatch, ctx, startup_health)

    report = run_startup_healthcheck(mode)

    assert report.has_failures
    failure = next(check for check in report.checks if check.name == expected_name)
    assert failure.status == "FAIL"
    assert expected_text in failure.detail


def test_ensure_healthy_startup_prints_report_and_exits_on_failure(
    monkeypatch,
    tmp_path,
    capsys,
):
    _configure_valid_startup(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="1"):
        ensure_healthy_startup("chat")

    captured = capsys.readouterr()
    assert captured.out == ""


def test_human_startup_report_is_concise_for_telegram(monkeypatch, tmp_path):
    _configure_valid_startup(monkeypatch, tmp_path)

    report = run_startup_healthcheck("telegram")
    human = report.render_human()

    assert "Startup check looks good for telegram." in human
    assert "PASS" not in human
    assert "SQLite write access verified" not in human
    assert "parsed 2 allowed telegram chat id(s)" in human.lower()
