#!/usr/bin/env python3
"""Generate the current Hub LangGraph diagram from the agent registry."""

from __future__ import annotations

import os
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

from agent_hub.registry import load_registry

DIAGRAM_DIR = ROOT / "docs" / "diagrams"
DIAGRAM_BASE = DIAGRAM_DIR / "07-HUB-LANGGRAPH-TOOLS"
MMD_PATH = DIAGRAM_BASE.with_suffix(".mmd")
SVG_PATH = DIAGRAM_BASE.with_suffix(".svg")

MEMORY_TOOL_NODES = [
    ("memory_hub_learnings", "Manage memory\n(hub learnings)"),
    ("memory_shared_docs", "Search memory\n(shared docs)"),
]


def sanitize_id(agent_id: str) -> str:
    return "agent_" + "_".join(agent_id.replace("-", "_").split())


def render_agent_nodes(registry: Iterable[object]) -> str:
    lines = []
    for spec in registry:
        node_id = sanitize_id(spec.id)
        label = f"{spec.name}<br/>({spec.id})"
        lines.append(f"    {node_id}[\"{label}\"]")
    return "\n".join(lines)


def build_mermaid(registry: Iterable[object]) -> str:
    agent_nodes = render_agent_nodes(registry)
    agent_ids = [sanitize_id(spec.id) for spec in registry]

    tool_links = "\n".join(f"    Hub --> {node_id}" for node_id in agent_ids)

    memory_nodes = "\n".join(
        f"    {node_id}[\"{label}\"]" for node_id, label in MEMORY_TOOL_NODES
    )

    if not agent_ids:
        agent_nodes = "    no_agents[\"No registered agents\"]"
        tool_links = "    Hub --> no_agents"

    return f"""flowchart TD
    You([You])
    Hub[\"Hub Orchestrator<br/>(LangGraph react agent)\"]

    subgraph Tools[\"Hub tools\"]
{memory_nodes}
{agent_nodes}
    end

    You --> Hub
    Hub --> memory_hub_learnings
    Hub --> memory_shared_docs
{tool_links}

    classDef note fill:#fff8dc,stroke:#e6a817;
    note[\"Generated from current registry and HubOrchestrator implementation\"]:::note
    Hub --> note
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
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")

    registry = load_registry()
    content = build_mermaid(registry)
    write_mermaid(content)
    try:
        render_svg()
    except subprocess.CalledProcessError as exc:
        print(f"SVG render failed: {exc}")


if __name__ == "__main__":
    main()
