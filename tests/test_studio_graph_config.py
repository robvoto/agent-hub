from __future__ import annotations

import json
from pathlib import Path


def test_langgraph_config_points_to_real_hub_graph() -> None:
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "langgraph.json").read_text(encoding="utf-8"))

    assert config["graphs"] == {
        "agent-hub": "./src/agent_hub/studio_graph.py:graph"
    }
    assert config["dependencies"] == ["."]
    assert config["env"] == "./.env"
    assert config["python_version"] == "3.13"


def test_studio_graph_matches_real_react_topology(monkeypatch) -> None:
    # Construct the graph without requiring a real credential; no API call is made.
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    from agent_hub.studio_graph import graph

    rendered = graph.get_graph()
    assert set(rendered.nodes) == {"__start__", "agent", "tools", "__end__"}
    edges = {(edge.source, edge.target) for edge in rendered.edges}
    assert edges == {
        ("__start__", "agent"),
        ("agent", "tools"),
        ("tools", "agent"),
        ("agent", "__end__"),
    }
