from __future__ import annotations

import json

import pytest

from agent_hub.human_mcp_gateway import HumanMCPError, _truncate_result, load_human_mcp_config


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
