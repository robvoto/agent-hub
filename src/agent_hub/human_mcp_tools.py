"""LangChain tool adapters for the bounded Human MCP gateway."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from .human_mcp_gateway import HumanMCPError, HumanMCPGateway, HumanMCPTool


def make_human_mcp_tools(
    gateway: HumanMCPGateway | None,
    *,
    session_id: str | None = None,
) -> list[StructuredTool]:
    if gateway is None:
        return []
    return [_make_tool(gateway, info, session_id=session_id) for info in gateway.list_tools()]


def _make_tool(
    gateway: HumanMCPGateway,
    info: HumanMCPTool,
    *,
    session_id: str | None,
) -> StructuredTool:
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
        use_isolated_context = _uses_isolated_browser_context(gateway, info)
        try:
            if use_isolated_context:
                return gateway.call_browser_tool(info.name, kwargs, session_id=session_id)
            return gateway.call_tool(info.name, kwargs)
        except HumanMCPError as exc:
            if use_isolated_context:
                return f"Human MCP tool '{info.name}' is unavailable: {exc}"
            raise

    description = info.description
    if _requires_approval(gateway, info):
        description += " This action pauses for explicit human approval before execution."
    return StructuredTool.from_function(
        func=_invoke,
        name=info.name,
        description=description,
        args_schema=_tool_input_schema(gateway, info),
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


def _uses_isolated_browser_context(gateway: HumanMCPGateway, info: HumanMCPTool) -> bool:
    """Select autonomous preparation only for the explicitly configured tools."""
    if not info.name.startswith("browser_") or not info.read_only:
        return False
    if _is_setup_tool(gateway, info):
        return False
    config = getattr(gateway, "config", None)
    browser_context = getattr(config, "browser_context", None)
    return bool(
        getattr(browser_context, "enabled", False)
        and info.name in getattr(browser_context, "auto_prepare_tools", ())
    )


def _tool_input_schema(gateway: HumanMCPGateway, info: HumanMCPTool) -> dict[str, Any]:
    """Keep MCP-owned context selectors out of autonomous model arguments."""
    if not _uses_isolated_browser_context(gateway, info):
        return info.input_schema
    browser_context = gateway.config.browser_context
    schema = deepcopy(info.input_schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop(browser_context.session_id_argument, None)
        properties.pop(browser_context.tab_id_argument, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [
            name
            for name in required
            if name
            not in {
                browser_context.session_id_argument,
                browser_context.tab_id_argument,
            }
        ]
    return schema
