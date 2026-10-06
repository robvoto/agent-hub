from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_hub import human_mcp_tools
from agent_hub.human_mcp_gateway import HumanMCPError, HumanMCPTool
from agent_hub.human_mcp_tools import make_human_mcp_tools


class _Gateway:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []
        self.config = SimpleNamespace(
            browser_context=SimpleNamespace(
                enabled=False,
                auto_prepare_tools=frozenset(),
            )
        )

    def list_tools(self):
        return self._tools

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return "ok"


def _tool(name: str, *, read_only: bool) -> HumanMCPTool:
    return HumanMCPTool(
        name=name,
        description=f"{name} description",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        read_only=read_only,
        destructive=not read_only,
        idempotent=True,
        open_world=True,
    )


def test_read_only_human_mcp_tool_calls_gateway_without_approval():
    gateway = _Gateway([_tool("docs_read_text", read_only=True)])
    tool = make_human_mcp_tools(gateway)[0]

    assert tool.invoke({"value": "doc-1"}) == "ok"
    assert gateway.calls == [("docs_read_text", {"value": "doc-1"})]


def test_browser_status_remains_direct_when_autonomous_context_is_disabled():
    gateway = _Gateway([_tool("browser_status", read_only=True)])
    tool = make_human_mcp_tools(gateway)[0]

    assert tool.invoke({"value": "status"}) == "ok"
    assert gateway.calls == [("browser_status", {"value": "status"})]


def test_browser_list_pages_remains_direct_when_autonomous_context_is_disabled():
    gateway = _Gateway([_tool("browser_list_pages", read_only=True)])
    tool = make_human_mcp_tools(gateway)[0]

    assert tool.invoke({"value": "pages"}) == "ok"
    assert gateway.calls == [("browser_list_pages", {"value": "pages"})]


def test_human_mcp_tool_description_flags_approval_for_mutations():
    gateway = _Gateway([_tool("docs_append_text", read_only=False)])
    tool = make_human_mcp_tools(gateway)[0]

    assert "explicit human approval" in tool.description


def test_mutating_human_mcp_tool_executes_only_after_approval(monkeypatch):
    gateway = _Gateway([_tool("docs_append_text", read_only=False)])
    monkeypatch.setattr(human_mcp_tools, "interrupt", lambda payload: True)
    tool = make_human_mcp_tools(gateway)[0]

    assert tool.invoke({"value": "append me"}) == "ok"
    assert gateway.calls == [("docs_append_text", {"value": "append me"})]


def test_mutating_human_mcp_tool_does_not_execute_after_rejection(monkeypatch):
    gateway = _Gateway([_tool("docs_append_text", read_only=False)])
    monkeypatch.setattr(human_mcp_tools, "interrupt", lambda payload: False)
    tool = make_human_mcp_tools(gateway)[0]

    result = tool.invoke({"value": "do not append"})

    assert "rejected" in result.lower()
    assert gateway.calls == []


def test_browser_setup_tool_stays_approval_gated_even_if_server_marks_read_only(monkeypatch):
    gateway = _Gateway([_tool("browser_open_session", read_only=True)])
    monkeypatch.setattr(human_mcp_tools, "interrupt", lambda payload: False)
    tool = make_human_mcp_tools(gateway)[0]

    result = tool.invoke({"value": "isolated"})

    assert "rejected" in result.lower()
    assert gateway.calls == []
    assert "explicit human approval" in tool.description


def test_browser_context_failure_becomes_clear_tool_result():
    class _FailingGateway(_Gateway):
        def call_browser_tool(self, name, arguments, *, session_id=None):
            raise HumanMCPError("No isolated agent-owned browser context is configured.")

    gateway = _FailingGateway([_tool("browser_snapshot", read_only=True)])
    gateway.config = SimpleNamespace(
        browser_context=SimpleNamespace(
            enabled=True,
            auto_prepare_tools=frozenset({"browser_snapshot"}),
            session_id_argument="session_id",
            tab_id_argument="tab_id",
        )
    )
    tool = make_human_mcp_tools(gateway, session_id="hub-a")[0]

    result = tool.invoke({"value": "read"})

    assert "unavailable" in result
    assert "No isolated agent-owned browser context" in result


def test_non_browser_human_mcp_failure_preserves_existing_error_path():
    class _FailingGateway(_Gateway):
        def call_tool(self, name, arguments):
            raise HumanMCPError("document service failed")

    gateway = _FailingGateway([_tool("docs_read_text", read_only=True)])
    tool = make_human_mcp_tools(gateway)[0]

    with pytest.raises(HumanMCPError, match="document service failed"):
        tool.invoke({"value": "read"})
