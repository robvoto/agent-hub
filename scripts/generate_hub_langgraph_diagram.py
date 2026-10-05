#!/usr/bin/env python3
"""Generate the current Hub LangGraph diagram from the agent registry."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))


DIAGRAM_DIR = ROOT / "docs" / "diagrams"
DIAGRAM_BASE = DIAGRAM_DIR / "05-HUB-LANGGRAPH-TOOLS"
MMD_PATH = DIAGRAM_BASE.with_suffix(".mmd")
SVG_PATH = DIAGRAM_BASE.with_suffix(".svg")

LANGGRAPH_TOOL_NODES = [
    (
        "search_shared_docs",
        "LangChain tool: search_shared_docs<br/>shared documentation search",
    ),
    (
        "parallel_specialist_fanout",
        "LangChain tool: parallel_specialist_fanout<br/>bounded multi-project specialist fan-out",
    ),
]


def sanitize_id(prefix: str, value: str) -> str:
    return prefix + "_" + "_".join(value.replace("-", "_").split())


def render_agent_nodes(registry: Iterable[object]) -> str:
    lines = []
    for spec in registry:
        node_id = sanitize_id("agent", spec.id)
        runtime_mode = (spec.runtime or {}).get("mode", "unknown")
        label = (
            f"LangChain tool: {spec.id}<br/>"
            f"Agent: {spec.name}<br/>"
            f"runtime: {runtime_mode}"
        )
        lines.append(f'    {node_id}["{label}"]')
    return "\n".join(lines)


def get_langgraph_tool_nodes() -> list[tuple[str, str]]:
    nodes = list(LANGGRAPH_TOOL_NODES)
    try:
        from agent_hub.human_mcp_gateway import load_human_mcp_config

        config = load_human_mcp_config()
        if config.enabled:
            groups = {
                "human_mcp_browser_navigation": [],
                "human_mcp_browser_actions": [],
                "human_mcp_docs": [],
                "human_mcp_gmail": [],
                "human_mcp_sheets": [],
            }
            action_names = {
                "browser_click",
                "browser_fill",
                "browser_press_key",
                "browser_upload_file",
            }
            for name in sorted(config.allowed_tools):
                if name in action_names:
                    groups["human_mcp_browser_actions"].append(name)
                elif name.startswith("browser_"):
                    groups["human_mcp_browser_navigation"].append(name)
                elif name.startswith("docs_"):
                    groups["human_mcp_docs"].append(name)
                elif name.startswith("gmail_"):
                    groups["human_mcp_gmail"].append(name)
                elif name.startswith("sheets_"):
                    groups["human_mcp_sheets"].append(name)

            labels = {
                "human_mcp_browser_navigation": "Human MCP — browser navigation/state",
                "human_mcp_browser_actions": "Human MCP — browser actions",
                "human_mcp_docs": "Human MCP — Google Docs",
                "human_mcp_gmail": "Human MCP — Gmail",
                "human_mcp_sheets": "Human MCP — Google Sheets",
            }
            for group_id, names in groups.items():
                if names:
                    exact_names = "<br/>".join(names)
                    nodes.append((group_id, f"{labels[group_id]}<br/>{exact_names}"))
    except Exception:
        # Diagram generation must not start or depend on the external gateway.
        pass
    return nodes


def build_mermaid(registry: Iterable[object]) -> str:
    agent_nodes = render_agent_nodes(registry)
    agent_ids = [sanitize_id("agent", spec.id) for spec in registry]
    tool_nodes = get_langgraph_tool_nodes()
    tool_node_ids = [sanitize_id("tool", tool_name) for tool_name, _ in tool_nodes]
    tool_links = "\n".join(f"    Hub --> {node_id}" for node_id in [*tool_node_ids, *agent_ids])
    rendered_tool_nodes = "\n".join(
        f'    {sanitize_id("tool", tool_name)}["{label}"]' for tool_name, label in tool_nodes
    )

    if not agent_ids:
        agent_nodes = '    no_agents["No registered agents"]'
        tool_links = "\n".join(
            [f"    Hub --> {node_id}" for node_id in tool_node_ids] + ["    Hub --> no_agents"]
        )

    commands_note = (
        "This is a callable-tool inventory, not a process-flow diagram.<br/>"
        "Human MCP names are grouped only for readability; names shown are exact.<br/>"
        "/learn, /memory, and /forget stay outside the tool list."
    )

    return f"""flowchart TD
    You([You])
    Hub[\"Hub Orchestrator<br/>(LangGraph react agent)\"]

    subgraph Tools[\"Tools callable from LangGraph\"]
{rendered_tool_nodes}
{agent_nodes}
    end

    You --> Hub
{tool_links}

    classDef note fill:#fff8dc,stroke:#e6a817;
    note[\"Generated from current registry and HubOrchestrator LangGraph tool wiring\"]:::note
    commands_note[\"{commands_note}\"]:::note
    Hub --> note
    Hub -.-> commands_note
"""


def write_mermaid(content: str) -> None:
    DIAGRAM_DIR.mkdir(parents=True, exist_ok=True)
    MMD_PATH.write_text(content, encoding="utf-8")
    print(f"Wrote Mermaid source to {MMD_PATH}")


def render_svg() -> None:
    npx = shutil.which("npx")
    if not npx:
        print("Skipping SVG render: npx not found.")
        return

    cmd = [npx, "--yes", "@mermaid-js/mermaid-cli", "-i", str(MMD_PATH), "-o", str(SVG_PATH)]
    print("Rendering SVG with Mermaid CLI...")
    subprocess.run(cmd, check=True)
    print(f"Wrote SVG to {SVG_PATH}")


def main() -> None:
    from agent_hub.factory_bridge import build_factory_agent_spec
    from agent_hub.registry import load_registry

    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")

    registry = load_registry()
    factory_spec = build_factory_agent_spec()
    if factory_spec is not None and not any(spec.id == factory_spec.id for spec in registry):
        registry.append(factory_spec)
    content = build_mermaid(registry)
    write_mermaid(content)
    try:
        render_svg()
    except subprocess.CalledProcessError as exc:
        print(f"SVG render failed: {exc}")


if __name__ == "__main__":
    main()
