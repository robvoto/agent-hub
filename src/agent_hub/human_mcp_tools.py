"""LangChain tool adapters for the bounded Human MCP gateway."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from .human_mcp_gateway import HumanMCPGateway, HumanMCPTool


def make_human_mcp_tools(gateway: HumanMCPGateway | None) -> list[StructuredTool]:
    if gateway is None:
        return []
    return [_make_tool(gateway, info) for info in gateway.list_tools()]


def _make_tool(gateway: HumanMCPGateway, info: HumanMCPTool) -> StructuredTool:
    def _invoke(**kwargs: Any) -> str:
        if not info.read_only:
            approved = interrupt(
                {
                    "kind": "human_mcp_approval",
                    "tool": info.name,
                    "arguments": kwargs,
                    "destructive": info.destructive,
                    "open_world": info.open_world,
                }
            )
            if approved is not True:
                return f"Human rejected Human MCP tool '{info.name}'. No external action was taken."
        return gateway.call_tool(info.name, kwargs)

    description = info.description
    if not info.read_only:
        description += " This action pauses for explicit human approval before execution."
    return StructuredTool.from_function(
        func=_invoke,
        name=info.name,
        description=description,
        args_schema=info.input_schema,
    )
