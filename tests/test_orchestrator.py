"""Tests for orchestrator task lifecycle tracking."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_hub.hub_memory import ExtractionCandidate, HubMemoryManager
from agent_hub.knowledge_store import SqliteStore
from agent_hub.orchestrator import (
    _SYSTEM_PROMPT,
    HubOrchestrator,
    _build_system_prompt,
    _dispatch_subprocess,
    _make_agent_tool,
    _repair_dangling_tool_calls,
    cancel_all_active_tasks,
)
from agent_hub.registry import AgentSpec
from agent_hub.task_control import TaskCancelled, get_task_control_registry
from agent_hub.task_runs import (
    DEFAULT_PROJECT_KEY,
    TASK_STATE_CANCELLED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_RECEIVED,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    active_task_run,
    get_task_run_store,
)


class _FakeGraph:
    def __init__(self, response: str = "Done", error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    def invoke(self, payload, config):
        if self._error is not None:
            raise self._error
        return {"messages": [SimpleNamespace(content=self._response)]}


class _FakeStreamingGraph:
    def __init__(self, response: str = "Done") -> None:
        self._response = response

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        yield ("tasks", {"name": "agent"})
        yield (
            "updates",
            {
                "agent": {
                    "messages": [
                        SimpleNamespace(content="", tool_calls=[{"name": "ai-tech-lead"}])
                    ]
                }
            },
        )
        yield ("tasks", {"name": "tools"})
        yield (
            "updates",
            {
                "tools": {
                    "messages": [SimpleNamespace(name="ai-tech-lead", content="Implemented change")]
                }
            },
        )
        yield ("tasks", {"name": "agent", "result": "done"})
        yield (
            "updates",
            {"agent": {"messages": [SimpleNamespace(content=self._response)]}},
        )
        yield ("values", {"messages": [SimpleNamespace(content=self._response)]})


class _FakeSingleNodeStreamingGraph:
    def __init__(self, response: str = "Done") -> None:
        self._response = response

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        yield ("tasks", {"name": "agent"})
        yield (
            "updates",
            {"agent": {"messages": [SimpleNamespace(content=self._response)]}},
        )
        yield ("values", {"messages": [SimpleNamespace(content=self._response)]})


class _FakeSnapshot:
    def __init__(self, values: dict) -> None:
        self.values = values


class _FakeCheckpointedGraph:
    """A minimal fake with just enough of the checkpointer surface to test
    dangling-tool-call repair: per-thread state plus get_state/update_state."""

    def __init__(self, response: str = "Done") -> None:
        self._response = response
        self.state: dict[str, list] = {}
        self.update_state_calls: list[tuple[str, dict]] = []

    def get_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return _FakeSnapshot({"messages": list(self.state.get(thread_id, []))})

    def update_state(self, config, values):
        thread_id = config["configurable"]["thread_id"]
        self.update_state_calls.append((thread_id, values))
        self.state.setdefault(thread_id, []).extend(values["messages"])

    def stream(self, payload, config, stream_mode):
        thread_id = config["configurable"]["thread_id"]
        self.state.setdefault(thread_id, []).extend(payload["messages"])
        response_message = SimpleNamespace(content=self._response, tool_calls=None)
        self.state[thread_id].append(response_message)
        yield ("values", {"messages": list(self.state[thread_id])})


def test_invoke_records_successful_task_run(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("All done"))

    orchestrator = HubOrchestrator()
    reply = orchestrator.invoke("Hello")

    assert reply == "All done"

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_SUCCEEDED
    assert runs[0].final_response == "All done"

    events = get_task_run_store().list_events(runs[0].id)
    assert [event.to_state for event in events] == [TASK_STATE_RECEIVED, TASK_STATE_SUCCEEDED]


def test_repair_dangling_tool_calls_closes_out_unanswered_call():
    graph = _FakeCheckpointedGraph()
    dangling = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "agent-factory",
                "args": {"task": "..."},
                "id": "call_abc",
                "type": "tool_call",
            }
        ],
    )
    graph.state["thread-1"] = [dangling]

    repaired = _repair_dangling_tool_calls(graph, "thread-1", "Interrupted before reply.")

    assert repaired == ["call_abc"]
    tool_messages = [m for m in graph.state["thread-1"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "call_abc"
    assert tool_messages[0].content == "Cancelled: Interrupted before reply."


def test_repair_dangling_tool_calls_is_idempotent():
    graph = _FakeCheckpointedGraph()
    dangling = AIMessage(
        content="",
        tool_calls=[{"name": "agent-factory", "args": {}, "id": "call_abc", "type": "tool_call"}],
    )
    graph.state["thread-1"] = [dangling]

    first = _repair_dangling_tool_calls(graph, "thread-1", "Interrupted before reply.")
    second = _repair_dangling_tool_calls(graph, "thread-1", "Interrupted before reply.")

    assert first == ["call_abc"]
    assert second == []
    assert len([m for m in graph.state["thread-1"] if isinstance(m, ToolMessage)]) == 1


def test_repair_dangling_tool_calls_ignores_already_answered_calls():
    graph = _FakeCheckpointedGraph()
    ai_message = AIMessage(
        content="",
        tool_calls=[{"name": "ai-tech-lead", "args": {}, "id": "call_xyz", "type": "tool_call"}],
    )
    graph.state["thread-1"] = [
        ai_message,
        ToolMessage(content="Done.", tool_call_id="call_xyz", name="ai-tech-lead"),
    ]

    repaired = _repair_dangling_tool_calls(graph, "thread-1", "Interrupted before reply.")

    assert repaired == []
    assert graph.update_state_calls == []


def test_repair_dangling_tool_calls_no_op_for_graph_without_checkpoint_api():
    assert _repair_dangling_tool_calls(_FakeGraph("Done"), "thread-1", "reason") == []


def test_invoke_repairs_dangling_tool_call_before_calling_the_graph(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    graph = _FakeCheckpointedGraph("All done")
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: graph)

    orchestrator = HubOrchestrator()
    thread_id = f"{orchestrator.session_id}:{DEFAULT_PROJECT_KEY}"
    dangling = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "agent-factory",
                "args": {"task": "Implement AF-052"},
                "id": "call_abc",
                "type": "tool_call",
            }
        ],
    )
    graph.state[thread_id] = [dangling]

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        reply = orchestrator.invoke("want you to code AF-052")

    assert reply == "All done"
    tool_messages = [m for m in graph.state[thread_id] if isinstance(m, ToolMessage)]
    assert any(m.tool_call_id == "call_abc" for m in tool_messages)
    assert "Hub repaired" in caplog.text


def test_invoke_leaves_clean_thread_state_untouched(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    graph = _FakeCheckpointedGraph("All done")
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: graph)

    orchestrator = HubOrchestrator()
    reply = orchestrator.invoke("Hello")

    assert reply == "All done"
    assert graph.update_state_calls == []


def test_invoke_emits_human_readable_progress_logs(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("All done"))

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        orchestrator.invoke("Hello")

    assert "Hub is deciding how to handle this request for project 'default'." in caplog.text
    assert "Hub has a final answer ready for the operator." in caplog.text
    assert "Hub lifecycle: [asked] -> [replied]" in caplog.text


def test_new_session_explains_reset_effect_in_human_log(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        orchestrator.new_session()

    assert "Future turns use a fresh LangGraph thread" in caplog.text
    assert "Existing active work is unchanged" in caplog.text
    assert "clean session-scoped controls" in caplog.text


def test_reset_session_stops_active_task_and_rotates_session(monkeypatch, caplog, tmp_path):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])

    orchestrator = HubOrchestrator()
    original_session_id = orchestrator.session_id
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id,
        user_message="Clean this up",
    )
    get_task_run_store().transition(
        run.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id=spec.id,
        dispatched_task="Delete the generated files",
        approval_token="approve-123",
    )

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        reply = orchestrator.reset_session()

    assert reply == "Reset complete. Stopped the active task and started a fresh conversation."
    assert orchestrator.session_id != original_session_id
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_CANCELLED
    assert updated.cancellation_reason == "Reset by user"
    assert "Operator requested stop for agent 'ai-tech-lead'" in caplog.text
    assert "Hub marked the task as cancelled." in caplog.text
    assert "Reset the hub conversation." in caplog.text


def test_restart_resumes_the_same_session_and_task_history(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("All done"))

    first = HubOrchestrator()
    first.invoke("Hello")

    # Simulate a process restart: a fresh HubOrchestrator instance backed by
    # the same (test-isolated) persisted stores.
    second = HubOrchestrator()

    assert second.session_id == first.session_id
    runs = get_task_run_store().list_runs(session_id=second.session_id)
    assert len(runs) == 1
    assert runs[0].final_response == "All done"


def test_new_session_rolls_pointer_forward_so_restart_resumes_new_session(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    first = HubOrchestrator()
    original_session_id = first.session_id
    first.new_session()
    rotated_session_id = first.session_id

    restarted = HubOrchestrator()

    assert restarted.session_id == rotated_session_id
    assert restarted.session_id != original_session_id


def test_reset_session_rolls_pointer_forward_so_restart_resumes_new_session(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    first = HubOrchestrator()
    original_session_id = first.session_id
    first.reset_session()
    rotated_session_id = first.session_id

    restarted = HubOrchestrator()

    assert restarted.session_id == rotated_session_id
    assert restarted.session_id != original_session_id


def test_first_ever_run_still_starts_cleanly_with_no_persisted_session(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("All done"))

    orchestrator = HubOrchestrator()

    assert orchestrator.session_id
    reply = orchestrator.invoke("Hello")
    assert reply == "All done"


def test_restart_resumes_project_and_learn_mode_selection(monkeypatch, tmp_path):
    from agent_hub.learning_mode import get_learning_mode_registry
    from agent_hub.project_context import get_project_context_registry

    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    first = HubOrchestrator()
    first.set_current_project(str(tmp_path))
    first.set_learning_mode(True)

    restarted = HubOrchestrator()

    assert restarted.session_id == first.session_id
    assert get_project_context_registry().get(restarted.session_id) == str(tmp_path.resolve())
    assert get_learning_mode_registry().is_enabled(restarted.session_id) is True


def test_invoke_stream_logs_langgraph_node_flow(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeStreamingGraph("All done"),
    )

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.DEBUG):
        reply = orchestrator.invoke("Hello")

    assert reply == "All done"

    # Node-by-node LangGraph tracing is technical detail — it belongs on the
    # debug logger, not the human-facing one (see test_..._human_log_stays_clean).
    debug_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "agent_hub.orchestrator"
    )
    assert "LangGraph entered node 'agent'." in debug_text
    assert "Node 'agent' requested tool call(s): ai-tech-lead." in debug_text
    assert "LangGraph rerouted from 'agent' to 'tools'." in debug_text
    assert "Node 'tools' produced message: Implemented change." in debug_text
    assert "LangGraph rerouted from 'tools' to 'agent'." in debug_text
    assert "LangGraph node path:" in debug_text
    assert "  [agent] -> [tools] -> [agent]" in debug_text
    assert "  |-- [agent] requested tool call(s): ai-tech-lead" in debug_text
    assert "  |-- [tools] produced message: Implemented change" in debug_text
    assert "  `-- [agent] produced message: All done" in debug_text
    assert debug_text.count("LangGraph node path:") == 1

    human_text = "\n".join(r.getMessage() for r in caplog.records if r.name == "agent_hub.human")
    assert "LangGraph" not in human_text


def test_invoke_stream_logs_single_langgraph_node_path(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeSingleNodeStreamingGraph("All done"),
    )

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.DEBUG):
        reply = orchestrator.invoke("Hello")

    assert reply == "All done"

    debug_text = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "agent_hub.orchestrator"
    )
    assert "LangGraph entered node 'agent'." in debug_text
    assert "Node 'agent' produced message: All done." in debug_text
    assert "LangGraph node path:" in debug_text
    assert "  [agent]" in debug_text
    assert "  `-- [agent] produced message: All done" in debug_text

    human_text = "\n".join(r.getMessage() for r in caplog.records if r.name == "agent_hub.human")
    assert "LangGraph" not in human_text


class _FakeToolResultStreamingGraph:
    """Mimics create_react_agent's real message types (not SimpleNamespace) for
    a single agent -> tools -> agent pass, so the final 'values' messages list
    can be inspected the same way _relay_specialist_terminal_message does."""

    def __init__(self, tool_message_content: str, paraphrase: str) -> None:
        self._tool_message_content = tool_message_content
        self._paraphrase = paraphrase

    def stream(self, payload, config, stream_mode):
        assert stream_mode == ["tasks", "updates", "values"]
        tool_call_message = AIMessage(
            content="", tool_calls=[{"name": "ai-tech-lead", "args": {}, "id": "call1"}]
        )
        tool_message = ToolMessage(content=self._tool_message_content, tool_call_id="call1")
        final_message = AIMessage(content=self._paraphrase)
        final_messages = [
            HumanMessage(content="code AF-052"),
            tool_call_message,
            tool_message,
            final_message,
        ]
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [tool_call_message]}})
        yield ("tasks", {"name": "tools"})
        yield ("updates", {"tools": {"messages": [tool_message]}})
        yield ("tasks", {"name": "agent"})
        yield ("updates", {"agent": {"messages": [final_message]}})
        yield ("values", {"messages": final_messages})


def test_invoke_relays_specialist_clarification_verbatim_instead_of_llm_paraphrase(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    tool_text = (
        "[AI Tech Lead] Clarification needed: Local docs found 0 relevant source(s) "
        "(minimum 2 required) for this complex task.\n"
        "Reason: Complexity check response invalid; requiring human approval."
    )
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeToolResultStreamingGraph(
            tool_text,
            "The AI Tech Lead requests clarification. Should I proceed?",
        ),
    )

    orchestrator = HubOrchestrator()
    reply = orchestrator.invoke("code AF-052")

    assert reply == tool_text


def test_invoke_relays_specialist_approval_request_verbatim(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    tool_text = (
        "[AI Tech Lead] Approval required: deletes files.\n"
        "Approval token: approve-123\n"
        "Use /approve to continue or /reject <reason> to stop."
    )
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeToolResultStreamingGraph(
            tool_text,
            "AI Tech Lead wants to delete some files — should I approve that?",
        ),
    )

    orchestrator = HubOrchestrator()
    reply = orchestrator.invoke("clean up")

    assert reply == tool_text


def test_invoke_keeps_llm_paraphrase_for_non_terminal_tool_results(monkeypatch):
    """Only needs_clarification/approval_required tool text is relayed verbatim —
    a normal success result should still go through the model's own reply."""
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeToolResultStreamingGraph(
            "[AI Tech Lead] Applied the fix.",
            "I applied the fix as requested.",
        ),
    )

    orchestrator = HubOrchestrator()
    reply = orchestrator.invoke("fix the bug")

    assert reply == "I applied the fix as requested."


def test_invoke_records_failed_task_run(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeGraph(error=RuntimeError("boom")),
    )

    orchestrator = HubOrchestrator()

    with pytest.raises(RuntimeError, match="boom"):
        orchestrator.invoke("Hello")

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_FAILED
    assert "boom" in (runs[0].error_message or "")


def test_agent_tool_records_routed_dispatched_and_waiting_approval(monkeypatch, tmp_path):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            input_path = Path(cmd[-3])
            output_path = Path(cmd[-1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            progress_path = Path(payload["progress_jsonl"])
            progress_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": payload["run_id"],
                        "request_id": payload["request_id"],
                        "sequence": 1,
                        "event_type": "waiting",
                        "phase": "awaiting-approval",
                        "human_summary": "Waiting for approval before deleting files.",
                        "occurred_at": "2026-07-23T00:00:00+00:00",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_path.write_text(
                json.dumps(
                    {
                        "status": "approval_required",
                        "summary": "Need approval before deleting files.",
                        "approval_token": "approve-123",
                    }
                ),
                encoding="utf-8",
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)

    run = get_task_run_store().create_run(session_id="session-1", user_message="Clean this up")
    tool = _make_agent_tool(spec)

    with active_task_run(run.id):
        reply = tool.invoke("Delete the generated files")

    assert "Approval required" in reply

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_WAITING_APPROVAL
    assert updated.selected_agent_id == "ai-tech-lead"
    assert updated.dispatched_task == "Delete the generated files"
    assert updated.approval_token == "approve-123"

    events = get_task_run_store().list_events(run.id)
    assert [event.to_state for event in events] == [
        TASK_STATE_RECEIVED,
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_WAITING_APPROVAL,
    ]


def test_agent_tool_preserves_approval_required_output_without_progress_events(
    monkeypatch, tmp_path
):
    """A legitimate approval_required/needs_clarification short-circuit must reach the
    user even if the specialist exited before emitting any progress.jsonl line — it
    is not the silent/broken run the progress-required gate exists to catch."""
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            output_path = Path(cmd[-1])
            output_path.write_text(
                json.dumps(
                    {
                        "status": "approval_required",
                        "summary": "Need approval before deleting files.",
                        "approval_token": "approve-123",
                    }
                ),
                encoding="utf-8",
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)

    run = get_task_run_store().create_run(session_id="session-1", user_message="Clean this up")
    tool = _make_agent_tool(spec)

    with active_task_run(run.id):
        reply = tool.invoke("Delete the generated files")

    assert "Approval required" in reply


def test_agent_tool_still_requires_progress_for_success_output(monkeypatch, tmp_path):
    """Unlike a legitimate short-circuit, a 'success' with zero progress events is the
    silent/broken-specialist case the progress-required gate must keep rejecting."""
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            output_path = Path(cmd[-1])
            output_path.write_text(
                json.dumps({"status": "success", "summary": "Done."}),
                encoding="utf-8",
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)

    run = get_task_run_store().create_run(session_id="session-1", user_message="Clean this up")
    tool = _make_agent_tool(spec)

    with active_task_run(run.id), pytest.raises(
        RuntimeError, match="finished without emitting any progress events"
    ):
        tool.invoke("Delete the generated files")


def test_agent_tool_emits_human_readable_specialist_logs(monkeypatch, tmp_path, caplog):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            input_path = Path(cmd[-3])
            output_path = Path(cmd[-1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            progress_path = Path(payload["progress_jsonl"])
            progress_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": payload["run_id"],
                        "request_id": payload["request_id"],
                        "sequence": 1,
                        "event_type": "phase",
                        "phase": "editing",
                        "human_summary": "Applying the requested change.",
                        "occurred_at": "2026-07-23T00:00:00+00:00",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output_path.write_text(
                json.dumps({"status": "success", "summary": "Applied the fix."}),
                encoding="utf-8",
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)

    run = get_task_run_store().create_run(session_id="session-1", user_message="Clean this up")
    tool = _make_agent_tool(spec)

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        with active_task_run(run.id):
            reply = tool.invoke("Delete the generated files")

    assert "Applied the fix." in reply
    assert "Routed to AI Tech Lead." in caplog.text
    assert "Calling AI Tech Lead (ai-tech-lead) with: Delete the generated files" in caplog.text
    assert "AI Tech Lead finished with status 'success'." in caplog.text
    # The cumulative "Hub lifecycle" summary is only worth a line at a
    # terminal/paused checkpoint — none of these intermediate active-state
    # transitions (routed/dispatched/in_progress) should each restate it.
    assert "Hub lifecycle" not in caplog.text
    # Dispatch/in-progress bookkeeping is real but not human-facing noise —
    # only the routing choice and the specialist's own start/finish lines are.
    assert "routed -> dispatched" not in caplog.text
    assert "dispatched -> in_progress" not in caplog.text


def test_approve_pending_resumes_subprocess_run(monkeypatch, tmp_path):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    calls: list[dict] = []

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            input_path = Path(cmd[-3])
            output_path = Path(cmd[-1])
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            calls.append(payload)
            progress_path = Path(payload["progress_jsonl"])
            progress_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": payload["run_id"],
                        "request_id": payload["request_id"],
                        "sequence": 1,
                        "event_type": "phase",
                        "phase": "resuming",
                        "human_summary": "Resuming the approved task.",
                        "occurred_at": "2026-07-23T00:00:00+00:00",
                        "metadata": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            if payload.get("human_approved"):
                output = {
                    "status": "success",
                    "summary": "Approved task completed.",
                }
            else:
                output = {
                    "status": "approval_required",
                    "summary": "Need approval before deleting files.",
                    "approval_token": "approve-123",
                }
            output_path.write_text(json.dumps(output), encoding="utf-8")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def communicate(self):
            return ("", "")

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id,
        user_message="Clean this up",
    )
    get_task_run_store().transition(
        run.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id=spec.id,
        dispatched_task="Delete the generated files",
        approval_token="approve-123",
        context_updates={"agent_request_id": "req-1"},
    )

    reply = orchestrator.approve_pending()

    assert "Approved task completed." in reply
    assert calls[0]["human_approved"] is True
    assert calls[0]["approval_token"] == "approve-123"
    assert calls[0]["request_id"] == "req-1"

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_SUCCEEDED


def test_stop_current_task_cancels_waiting_approval_run(monkeypatch, tmp_path):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id,
        user_message="Clean this up",
    )
    get_task_run_store().transition(
        run.id,
        TASK_STATE_WAITING_APPROVAL,
        selected_agent_id=spec.id,
        dispatched_task="Delete the generated files",
        approval_token="approve-123",
    )

    reply = orchestrator.stop_current_task()

    assert f"Stopped run {run.id}" in reply
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_CANCELLED
    assert updated.cancellation_reason == "Stopped by user"


def test_stop_current_task_terminates_active_subprocess(monkeypatch, tmp_path):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    started = threading.Event()
    terminated = threading.Event()
    result: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            self.returncode = None
            started.set()

        def poll(self):
            return self.returncode

        def communicate(self):
            return ("", "terminated")

        def terminate(self):
            self.returncode = -15
            terminated.set()

        def wait(self, timeout=None):
            self.returncode = -15
            terminated.set()
            return self.returncode

        def kill(self):
            self.returncode = -9
            terminated.set()

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id,
        user_message="Delete the generated files",
    )
    get_task_control_registry().register_run(run.id)

    def _invoke() -> None:
        try:
            with active_task_run(run.id):
                _dispatch_subprocess(spec, "Delete the generated files")
        except TaskCancelled as exc:
            result["cancelled"] = exc.reason
        finally:
            get_task_control_registry().unregister_run(run.id)

    worker = threading.Thread(target=_invoke)
    worker.start()
    assert started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline:
        handle = get_task_control_registry().get_handle(run.id)
        if handle is not None and handle.process is not None:
            break
        time.sleep(0.01)

    reply = orchestrator.stop_current_task()
    worker.join(timeout=2)

    assert terminated.is_set()
    assert "Stopped run" in reply
    assert result["cancelled"] == "Stopped by user"

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_CANCELLED
    assert runs[0].selected_agent_id == "ai-tech-lead"


def test_cancel_all_active_tasks_terminates_subprocess_and_marks_run_cancelled(
    monkeypatch, tmp_path
):
    spec = AgentSpec(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Implements code changes",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )

    started = threading.Event()
    terminated = threading.Event()
    result: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
            self.returncode = None
            started.set()

        def poll(self):
            return self.returncode

        def communicate(self):
            return ("", "terminated")

        def terminate(self):
            self.returncode = -15
            terminated.set()

        def wait(self, timeout=None):
            self.returncode = -15
            terminated.set()
            return self.returncode

        def kill(self):
            self.returncode = -9
            terminated.set()

    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _FakePopen)

    run = get_task_run_store().create_run(
        session_id="shutdown-session",
        user_message="Delete the generated files",
    )
    get_task_control_registry().register_run(run.id)

    def _invoke() -> None:
        try:
            with active_task_run(run.id):
                _dispatch_subprocess(spec, "Delete the generated files")
        except TaskCancelled as exc:
            result["cancelled"] = exc.reason
        finally:
            get_task_control_registry().unregister_run(run.id)

    worker = threading.Thread(target=_invoke)
    worker.start()
    assert started.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline:
        handle = get_task_control_registry().get_handle(run.id)
        if handle is not None and handle.process is not None:
            break
        time.sleep(0.01)

    cancelled_run_ids = cancel_all_active_tasks("Hub shut down.")
    worker.join(timeout=2)

    assert terminated.is_set()
    assert cancelled_run_ids == [run.id]
    assert result["cancelled"] == "Hub shut down."
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_CANCELLED
    assert updated.cancellation_reason == "Hub shut down."


def test_cancel_all_active_tasks_is_a_no_op_when_nothing_is_running():
    assert cancel_all_active_tasks("Hub shut down.") == []


def test_build_system_prompt_injects_stored_learnings(monkeypatch, tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    manager.learn("Prefer subprocess dispatch for AI Tech Lead.", source="cli")
    monkeypatch.setattr("agent_hub.orchestrator.HubMemoryManager", lambda: manager)

    messages = _build_system_prompt({"messages": [HumanMessage(content="hi")]})

    assert _SYSTEM_PROMPT in messages[0].content
    assert "Prefer subprocess dispatch for AI Tech Lead." in messages[0].content
    assert messages[-1].content == "hi"


def test_build_system_prompt_without_learnings_uses_base_prompt(monkeypatch, tmp_path):
    manager = HubMemoryManager(SqliteStore(tmp_path / "knowledge.sqlite3"))
    monkeypatch.setattr("agent_hub.orchestrator.HubMemoryManager", lambda: manager)

    messages = _build_system_prompt({"messages": []})

    assert messages[0].content == _SYSTEM_PROMPT


def test_run_learning_pass_stores_high_confidence_candidate(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    calls: list[tuple] = []

    def fake_extractor(conversation_text, existing_active_auto, existing_active_operator=()):
        calls.append((conversation_text, existing_active_auto, existing_active_operator))
        return [
            ExtractionCandidate(
                action="add", value="Prefers tabs over spaces.", supersedes_id=None, confidence="high"
            ),
            ExtractionCandidate(
                action="add", value="Maybe likes dark mode?", supersedes_id=None, confidence="low"
            ),
        ]

    orchestrator = HubOrchestrator(semantic_extractor=fake_extractor)
    store = get_task_run_store()
    run = store.create_run(session_id=orchestrator.session_id, user_message="Use tabs please")
    store.transition(
        run.id,
        TASK_STATE_SUCCEEDED,
        detail="done",
        final_response="Sure, using tabs from now on.",
    )

    messages = orchestrator.run_learning_pass(orchestrator.session_id)

    assert len(calls) == 1
    assert "Use tabs please" in calls[0][0]
    assert messages == ["\U0001f9e0 Learned: Prefers tabs over spaces."]

    records = HubMemoryManager().list_learnings(types=["semantic"])
    stored = [r for r in records if r.scope == "auto"]
    assert len(stored) == 1
    assert stored[0].value == "Prefers tabs over spaces."
    assert stored[0].status == "active"
    assert run.id in stored[0].evidence


def test_run_learning_pass_shows_extractor_existing_operator_records(monkeypatch):
    """The extractor must see Rob's explicit /learn facts, not just auto ones,
    so it can avoid duplicating or conflicting with them."""
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    calls: list[tuple] = []

    def fake_extractor(conversation_text, existing_active_auto, existing_active_operator=()):
        calls.append((existing_active_auto, existing_active_operator))
        return []

    orchestrator = HubOrchestrator(semantic_extractor=fake_extractor)
    HubMemoryManager().learn("Prefers Telegram for operator control.", source="cli")

    store = get_task_run_store()
    run = store.create_run(session_id=orchestrator.session_id, user_message="hello")
    store.transition(run.id, TASK_STATE_SUCCEEDED, final_response="hi")

    orchestrator.run_learning_pass(orchestrator.session_id)

    assert len(calls) == 1
    existing_auto, existing_operator = calls[0]
    assert existing_auto == []
    assert len(existing_operator) == 1
    assert existing_operator[0].value == "Prefers Telegram for operator control."


def test_run_learning_pass_never_disables_operator_record(monkeypatch):
    """Even if the model proposes superseding an explicit /learn record, Hub
    must refuse — only Rob can change an operator-established fact."""
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    operator_record = HubMemoryManager().learn("Prefers tabs over spaces.", source="cli")

    def rogue_extractor(conversation_text, existing_active_auto, existing_active_operator=()):
        return [
            ExtractionCandidate(
                action="update",
                value="Actually prefers spaces now.",
                supersedes_id=operator_record.identifier,
                confidence="high",
            )
        ]

    orchestrator = HubOrchestrator(semantic_extractor=rogue_extractor)
    store = get_task_run_store()
    run = store.create_run(session_id=orchestrator.session_id, user_message="Use spaces please")
    store.transition(run.id, TASK_STATE_SUCCEEDED, final_response="ok")

    messages = orchestrator.run_learning_pass(orchestrator.session_id)

    assert messages == []
    records = {r.identifier: r for r in HubMemoryManager().list_learnings(types=["semantic"])}
    assert records[operator_record.identifier].status == "active"
    assert not any(r.value == "Actually prefers spaces now." for r in records.values())


def test_run_learning_pass_is_noop_with_no_new_completed_runs(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    calls: list = []
    orchestrator = HubOrchestrator(
        semantic_extractor=lambda text, existing_auto, existing_operator=(): calls.append(1) or []
    )

    messages = orchestrator.run_learning_pass(orchestrator.session_id)

    assert messages == []
    assert calls == []


def test_run_learning_pass_only_processes_runs_after_watermark(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    call_texts: list[str] = []

    def fake_extractor(conversation_text, existing_active_auto, existing_active_operator=()):
        call_texts.append(conversation_text)
        return []

    orchestrator = HubOrchestrator(semantic_extractor=fake_extractor)
    store = get_task_run_store()

    run1 = store.create_run(session_id=orchestrator.session_id, user_message="First message")
    store.transition(run1.id, TASK_STATE_SUCCEEDED, final_response="ok1")
    orchestrator.run_learning_pass(orchestrator.session_id)

    run2 = store.create_run(session_id=orchestrator.session_id, user_message="Second message")
    store.transition(run2.id, TASK_STATE_SUCCEEDED, final_response="ok2")
    orchestrator.run_learning_pass(orchestrator.session_id)

    assert len(call_texts) == 2
    assert "First message" not in call_texts[1]
    assert "Second message" in call_texts[1]


def test_invoke_rejects_new_task_when_same_project_already_busy(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("Should not run"))

    orchestrator = HubOrchestrator()
    store = get_task_run_store()
    busy_run = store.create_run(session_id="some-other-session", user_message="first task")
    store.update_run(busy_run.id, context_updates={"target_project": DEFAULT_PROJECT_KEY})
    store.transition(busy_run.id, TASK_STATE_IN_PROGRESS, detail="running")

    reply = orchestrator.invoke("second task, same default project")

    assert "already running" in reply
    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert runs == []


def test_invoke_allows_task_for_a_different_project(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("Done for B"))

    orchestrator = HubOrchestrator()
    store = get_task_run_store()
    busy_run = store.create_run(session_id="some-other-session", user_message="first task")
    store.update_run(busy_run.id, context_updates={"target_project": "/repo/a"})
    store.transition(busy_run.id, TASK_STATE_IN_PROGRESS, detail="running")

    project_b = tmp_path / "repo-b"
    project_b.mkdir()
    orchestrator.set_current_project(str(project_b))

    reply = orchestrator.invoke("second task, different project")

    assert reply == "Done for B"


def test_pending_run_disambiguates_by_currently_selected_project(monkeypatch, tmp_path):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("unused"))

    orchestrator = HubOrchestrator()
    store = get_task_run_store()

    run_a = store.create_run(session_id=orchestrator.session_id, user_message="task for A")
    store.update_run(run_a.id, context_updates={"target_project": "/repo/a"})
    store.transition(run_a.id, TASK_STATE_WAITING_APPROVAL, approval_token="token-a")

    project_b = tmp_path / "repo-b"
    project_b.mkdir()
    run_b = store.create_run(session_id=orchestrator.session_id, user_message="task for B")
    store.update_run(run_b.id, context_updates={"target_project": str(project_b.resolve())})
    store.transition(run_b.id, TASK_STATE_WAITING_APPROVAL, approval_token="token-b")

    # No /project selected yet — default project has no pending run of its own.
    assert orchestrator.pending_run() is None

    orchestrator.set_current_project(str(project_b))
    pending = orchestrator.pending_run()
    assert pending is not None
    assert pending.id == run_b.id
    assert pending.approval_token == "token-b"
