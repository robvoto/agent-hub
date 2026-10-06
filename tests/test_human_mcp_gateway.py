from __future__ import annotations

import json
import logging
import threading

import pytest

from agent_hub.human_mcp_gateway import (
    HumanMCPError,
    HumanMCPGateway,
    HumanMCPTool,
    _truncate_result,
    load_human_mcp_config,
)


def test_load_human_mcp_config_requires_explicit_allowlist(tmp_path):
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "transport": {"command": "powershell.exe", "args": []},
                "allowed_tools": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HumanMCPError, match="allowed_tools"):
        load_human_mcp_config(path)


def test_load_human_mcp_config_round_trips_transport_and_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_HUMAN_MCP_ENABLED", raising=False)
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "transport": {"command": "powershell.exe", "args": ["-NoProfile"]},
                "allowed_tools": ["browser_snapshot"],
                "connect_timeout_seconds": 9,
                "call_timeout_seconds": 11,
                "max_result_chars": 1234,
            }
        ),
        encoding="utf-8",
    )

    config = load_human_mcp_config(path)

    assert config.enabled is True
    assert config.command == "powershell.exe"
    assert config.args == ("-NoProfile",)
    assert config.allowed_tools == frozenset({"browser_snapshot"})
    assert config.connect_timeout_seconds == 9
    assert config.call_timeout_seconds == 11
    assert config.max_result_chars == 1234
    assert config.browser_context.enabled is False


def test_load_human_mcp_config_uses_30_second_connect_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_HUMAN_MCP_ENABLED", raising=False)
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "transport": {"command": "powershell.exe", "args": []},
                "allowed_tools": ["browser_snapshot"],
            }
        ),
        encoding="utf-8",
    )

    assert load_human_mcp_config(path).connect_timeout_seconds == 30


def test_human_mcp_enabled_env_overrides_repo_default(tmp_path, monkeypatch):
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": False,
                "transport": {"command": "powershell.exe", "args": []},
                "allowed_tools": ["browser_snapshot"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HUB_HUMAN_MCP_ENABLED", "true")

    assert load_human_mcp_config(path).enabled is True


def test_truncate_result_is_bounded():
    assert _truncate_result("abc", 10) == "abc"
    assert _truncate_result("abcdefghij", 5).startswith("abcde\n...<truncated 5 chars>")


def _browser_context_config(tmp_path, *, max_calls_per_task=12):
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "transport": {"command": "powershell.exe", "args": []},
                "allowed_tools": [
                    "browser_open_session",
                    "browser_open_tab",
                    "browser_snapshot",
                ],
                "browser_context": {
                    "enabled": True,
                    "max_calls_per_task": max_calls_per_task,
                    "auto_prepare_tools": ["browser_snapshot"],
                    "session_open_arguments": {
                        "isolation": "isolated",
                        "owner": "agent",
                    },
                    "tab_open_arguments": {
                        "session_id": "$session_id",
                        "url": "about:blank",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return load_human_mcp_config(path)


def _browser_gateway(tmp_path, responses=None, *, max_calls_per_task=12):
    config = _browser_context_config(tmp_path, max_calls_per_task=max_calls_per_task)
    gateway = HumanMCPGateway.__new__(HumanMCPGateway)
    gateway.config = config
    gateway._tools = {
        "browser_open_session": HumanMCPTool(
            name="browser_open_session",
            description="open isolated session",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"isolation": {}, "owner": {}},
                "required": ["isolation", "owner"],
            },
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=False,
        ),
        "browser_open_tab": HumanMCPTool(
            name="browser_open_tab",
            description="open tab",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"session_id": {}, "url": {}},
                "required": ["session_id", "url"],
            },
            read_only=False,
            destructive=False,
            idempotent=True,
            open_world=False,
        ),
        "browser_snapshot": HumanMCPTool(
            name="browser_snapshot",
            description="read page",
            input_schema={"type": "object", "properties": {}},
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=True,
        ),
    }
    gateway._browser_context = None
    gateway._browser_context_lock = threading.RLock()
    gateway._browser_call_counts = {}
    calls = []
    results = responses or {
        "browser_open_session": json.dumps(
            {"session_id": "session-1", "isolated": True, "owner": "agent"}
        ),
        "browser_open_tab": json.dumps({"tab_id": "tab-1", "session_id": "session-1"}),
        "browser_snapshot": "snapshot",
    }

    def call_tool(name, arguments):
        calls.append((name, arguments))
        result = results[name]
        if isinstance(result, Exception):
            raise result
        return result

    gateway.call_tool = call_tool
    return gateway, calls


def test_browser_snapshot_missing_tab_prepares_isolated_context(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    gateway, calls = _browser_gateway(tmp_path)

    assert gateway.call_browser_tool("browser_snapshot", {}) == "snapshot"
    assert [name for name, _ in calls] == [
        "browser_open_session",
        "browser_open_tab",
        "browser_snapshot",
    ]
    assert calls[0][1] == {"isolation": "isolated", "owner": "agent"}
    assert calls[1][1] == {"session_id": "session-1", "url": "about:blank"}
    assert "Human MCP browser audit" in caplog.text


def test_browser_context_reuses_isolated_session_and_tab(tmp_path):
    gateway, calls = _browser_gateway(tmp_path)

    gateway.call_browser_tool("browser_snapshot", {})
    gateway.call_browser_tool("browser_snapshot", {})

    assert [name for name, _ in calls] == [
        "browser_open_session",
        "browser_open_tab",
        "browser_snapshot",
        "browser_snapshot",
    ]


def test_browser_context_fails_closed_without_isolation_evidence(tmp_path):
    gateway, _ = _browser_gateway(
        tmp_path,
        responses={
            "browser_open_session": json.dumps({"session_id": "session-1", "owner": "agent"}),
            "browser_open_tab": json.dumps({"tab_id": "tab-1", "session_id": "session-1"}),
            "browser_snapshot": "snapshot",
        },
    )

    with pytest.raises(HumanMCPError, match="did not prove.*isolated"):
        gateway.call_browser_tool("browser_snapshot", {})


def test_browser_context_failure_does_not_retry_or_use_existing_tab(tmp_path):
    gateway, calls = _browser_gateway(
        tmp_path,
        responses={
            "browser_open_session": HumanMCPError("MCP setup failed"),
            "browser_open_tab": json.dumps({"tab_id": "tab-1", "session_id": "session-1"}),
            "browser_snapshot": "snapshot",
        },
    )

    with pytest.raises(HumanMCPError, match="MCP setup failed"):
        gateway.call_browser_tool("browser_snapshot", {})
    assert [name for name, _ in calls] == ["browser_open_session"]
    assert gateway._browser_context is None


def test_browser_context_enforces_per_task_call_bound(tmp_path):
    from agent_hub.task_runs import active_task_run

    gateway, _ = _browser_gateway(tmp_path, max_calls_per_task=3)

    with active_task_run("run-059"):
        gateway.call_browser_tool("browser_snapshot", {})
        with pytest.raises(HumanMCPError, match="call limit"):
            gateway.call_browser_tool("browser_snapshot", {})


def test_enabled_browser_context_requires_explicit_isolation_arguments(tmp_path):
    path = tmp_path / "human_mcp.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "transport": {"command": "powershell.exe", "args": []},
                "allowed_tools": ["browser_open_session", "browser_open_tab", "browser_snapshot"],
                "browser_context": {
                    "enabled": True,
                    "auto_prepare_tools": ["browser_snapshot"],
                    "session_open_arguments": {},
                    "tab_open_arguments": {"session_id": "$session_id"},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HumanMCPError, match="isolated mode"):
        load_human_mcp_config(path)
