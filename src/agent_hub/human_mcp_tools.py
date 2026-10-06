"""LangChain tool adapters for the bounded Human MCP gateway."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from .human_mcp_gateway import HumanMCPError, HumanMCPGateway, HumanMCPTool


def make_human_mcp_tools(gateway: HumanMCPGateway | None) -> list[StructuredTool]:
    if gateway is None:
        return []
    return [_make_tool(gateway, info) for info in gateway.list_tools()]


def _make_tool(gateway: HumanMCPGateway, info: HumanMCPTool) -> StructuredTool:
    def _invoke(**kwargs: Any) -> str:
        if _requires_approval(gateway, info):
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
        try:
            if info.name.startswith("browser_") and info.read_only and not _is_setup_tool(
                gateway, info
            ):
                return gateway.call_browser_tool(info.name, kwargs)
            return gateway.call_tool(info.name, kwargs)
        except HumanMCPError as exc:
            if info.name.startswith("browser_") and info.read_only and not _is_setup_tool(
                gateway, info
            ):
                return f"Human MCP tool '{info.name}' is unavailable: {exc}"
            raise

    description = info.description
    if _requires_approval(gateway, info):
        description += " This action pauses for explicit human approval before execution."
    return StructuredTool.from_function(
        func=_invoke,
        name=info.name,
        description=description,
        args_schema=info.input_schema,
    )


def _requires_approval(gateway: HumanMCPGateway, info: HumanMCPTool) -> bool:
    """Keep setup operations gated even when a server labels them read-only."""
    if not info.read_only:
        return True
    return _is_setup_tool(gateway, info)


def _is_setup_tool(gateway: HumanMCPGateway, info: HumanMCPTool) -> bool:
    config = getattr(gateway, "config", None)
    browser_context = getattr(config, "browser_context", None)
    setup_names = {
        getattr(browser_context, "session_tool", "browser_open_session"),
        getattr(browser_context, "tab_tool", "browser_open_tab"),
    }
    return info.name in setup_names
