from __future__ import annotations

import importlib.util
from pathlib import Path

from agent_hub.registry import AgentSpec


def _load_diagram_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "generate_hub_langgraph_diagram.py"
    )
    spec = importlib.util.spec_from_file_location("generate_hub_langgraph_diagram", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_mermaid_lists_real_langgraph_tools_and_not_learn_commands():
    module = _load_diagram_module()
    registry = [
        AgentSpec(
            id="ai-tech-lead",
            name="AI Tech Lead",
            purpose="Implement bounded coding tasks.",
            runtime={},
        )
    ]

    mermaid = module.build_mermaid(registry)

    assert 'tool_search_shared_docs["Search shared docs<br/>(hub + factory)"]' in mermaid
    assert "Hub --> tool_search_shared_docs" in mermaid
    assert 'agent_ai_tech_lead["AI Tech Lead<br/>(ai-tech-lead)"]' in mermaid
    assert "memory_hub_learnings" not in mermaid
    assert "/learn, /memory, and /forget are operator commands" in mermaid


def test_build_mermaid_keeps_no_agents_placeholder():
    module = _load_diagram_module()

    mermaid = module.build_mermaid([])

    assert 'no_agents["No registered agents"]' in mermaid
    assert "Hub --> tool_search_shared_docs" in mermaid
