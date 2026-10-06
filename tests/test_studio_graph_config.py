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


def test_studio_graph_exposes_hub_lifecycle_boundary() -> None:
    from agent_hub.studio_graph import graph

    rendered = graph.get_graph()
    assert set(rendered.nodes) == {"__start__", "hub", "__end__"}
    edges = {(edge.source, edge.target) for edge in rendered.edges}
    assert edges == {
        ("__start__", "hub"),
        ("hub", "__end__"),
    }


def test_studio_graph_uses_studio_thread_as_hub_session(monkeypatch) -> None:
    import asyncio

    import agent_hub.studio_graph as studio_graph

    calls: list[tuple[str, str]] = []

    class FakeHub:
        def __init__(self, *, session_id: str) -> None:
            self.session_id = session_id

        def pending_run(self):
            return None

        def invoke(self, message: str) -> str:
            calls.append((self.session_id, message))
            return "Hub reply"

    monkeypatch.setattr(studio_graph, "HubOrchestrator", FakeHub)
    result = asyncio.run(
        studio_graph._invoke_hub(
            {"messages": [{"role": "user", "content": "Hello Hub"}]},
            {"configurable": {"thread_id": "studio-thread-1"}},
        )
    )

    assert result["messages"][0].content == "Hub reply"
    assert result["hub_session_id"] == "studio:studio-thread-1"
    assert calls == [("studio:studio-thread-1", "Hello Hub")]


def test_studio_graph_accepts_langsmith_text_content_blocks(monkeypatch) -> None:
    import asyncio

    import agent_hub.studio_graph as studio_graph

    calls: list[str] = []

    class FakeHub:
        def __init__(self, *, session_id: str) -> None:
            self.session_id = session_id

        def pending_run(self):
            return None

        def invoke(self, message: str) -> str:
            calls.append(message)
            return "Hub reply"

    monkeypatch.setattr(studio_graph, "HubOrchestrator", FakeHub)
    asyncio.run(
        studio_graph._invoke_hub(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello from Chrome"}],
                    }
                ]
            },
            {"configurable": {"thread_id": "studio-thread-2"}},
        )
    )

    assert calls == ["Hello from Chrome"]


def test_studio_turn_resumes_pending_cli_telegram_lifecycle(monkeypatch) -> None:
    import agent_hub.studio_graph as studio_graph

    calls: list[tuple[str, str]] = []

    class FakeHub:
        def pending_run(self):
            return type("Pending", (), {"state": "waiting_clarification"})()

        def provide_clarification(self, message: str) -> str:
            calls.append(("clarification", message))
            return "clarification resumed"

        def invoke(self, message: str) -> str:
            calls.append(("invoke", message))
            return "new task"

    hub = FakeHub()
    assert studio_graph._process_operator_turn(hub, "Use the Hub project") == (
        "clarification resumed"
    )
    assert calls == [("clarification", "Use the Hub project")]


def test_studio_turn_resumes_pending_decision_without_new_invocation() -> None:
    import agent_hub.studio_graph as studio_graph

    calls: list[tuple[str, str]] = []

    class FakeHub:
        def pending_run(self):
            return type("Pending", (), {"state": "waiting_decision"})()

        def provide_decision_reply(self, message: str) -> str:
            calls.append(("decision", message))
            return "decision resumed"

        def invoke(self, message: str) -> str:
            calls.append(("invoke", message))
            raise AssertionError("a decision reply must not start a new invocation")

    assert studio_graph._process_operator_turn(FakeHub(), "2") == "decision resumed"
    assert calls == [("decision", "2")]


def test_studio_turn_routes_approval_rejection_and_stop_to_hub_methods() -> None:
    import agent_hub.studio_graph as studio_graph

    calls: list[tuple[str, object]] = []

    class FakeHub:
        def approve_pending(self):
            calls.append(("approve", None))
            return "approved"

        def reject_pending(self, reason: str):
            calls.append(("reject", reason))
            return "rejected"

        def stop_current_task(self, *, identifier=None):
            calls.append(("stop", identifier))
            return "stopped"

    hub = FakeHub()
    assert studio_graph._process_operator_turn(hub, "/approve") == "approved"
    assert studio_graph._process_operator_turn(hub, "/reject no thanks") == "rejected"
    assert studio_graph._process_operator_turn(hub, "/stop abc123") == "stopped"
    assert calls == [
        ("approve", None),
        ("reject", "no thanks"),
        ("stop", "abc123"),
    ]


def test_studio_turn_does_not_dispatch_unsupported_commands() -> None:
    import agent_hub.studio_graph as studio_graph

    class FakeHub:
        def invoke(self, message: str) -> str:
            raise AssertionError("unsupported commands must not dispatch")

    assert "Unsupported Studio command" in studio_graph._process_operator_turn(
        FakeHub(), "/learn secretly dispatch this"
    )
