from __future__ import annotations

from agent_hub import human_mcp_tools
from agent_hub.human_mcp_gateway import HumanMCPTool
from agent_hub.human_mcp_tools import make_human_mcp_tools


class _Gateway:
    def __init__(self, tools):
        self._tools = tools
        self.calls = []

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
