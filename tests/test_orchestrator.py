"""Tests for orchestrator task lifecycle tracking."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

from agent_hub.hub_memory import ExtractionCandidate, HubMemoryManager
from agent_hub.knowledge_store import SqliteStore
from agent_hub.orchestrator import (
    _SYSTEM_PROMPT,
    HubOrchestrator,
    _build_system_prompt,
    _dispatch_subprocess,
    _make_agent_tool,
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


def test_invoke_emits_human_readable_progress_logs(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self: _FakeGraph("All done"))

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        orchestrator.invoke("Hello")

    assert "Hub is deciding how to handle this request for project 'default'." in caplog.text
    assert "Hub has a final answer ready for the operator." in caplog.text


def test_invoke_stream_logs_langgraph_node_flow(monkeypatch, caplog):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self: _FakeStreamingGraph("All done"),
    )

    orchestrator = HubOrchestrator()

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        reply = orchestrator.invoke("Hello")

    assert reply == "All done"
    assert "LangGraph task 'agent' started." in caplog.text
    assert "LangGraph entered node 'agent'." in caplog.text
    assert "Node 'agent' requested tool call(s): ai-tech-lead." in caplog.text
    assert "LangGraph rerouted from 'agent' to 'tools'." in caplog.text
    assert "Node 'tools' produced message: Implemented change." in caplog.text
    assert "LangGraph rerouted from 'tools' to 'agent'." in caplog.text


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
        def __init__(self, cmd, cwd, stdout, stderr, text):
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
        def __init__(self, cmd, cwd, stdout, stderr, text):
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
    assert "Hub chose AI Tech Lead (ai-tech-lead)." in caplog.text
    assert "Calling AI Tech Lead (ai-tech-lead) with: Delete the generated files" in caplog.text
    assert "AI Tech Lead finished with status 'success'." in caplog.text


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
        def __init__(self, cmd, cwd, stdout, stderr, text):
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
        def __init__(self, cmd, cwd, stdout, stderr, text):
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

    reply = orchestrator.stop_current_task()
    worker.join(timeout=2)

    assert terminated.is_set()
    assert "Stopped run" in reply
    assert result["cancelled"] == "Stopped by user"

    runs = get_task_run_store().list_runs(session_id=orchestrator.session_id)
    assert len(runs) == 1
    assert runs[0].state == TASK_STATE_CANCELLED
    assert runs[0].selected_agent_id == "ai-tech-lead"


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
