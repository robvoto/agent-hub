"""AGENT-HUB-039: Hub resolves the operator's /project selection to a
canonical ProjectContext (project_id, contract version, fingerprint) and
revalidates it fresh before every dispatch, stopping the dispatch with a
clear error instead of silently sending a stale/mismatched selection. A
resumed dispatch replays the exact context pinned at the original dispatch."""

from __future__ import annotations

import json
from pathlib import Path

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import AgentSpec
from agent_hub.task_runs import (
    TASK_STATE_FAILED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    active_task_run,
    get_task_run_store,
)

SPECIALIST_ID = "project-aware-agent"


class _UnusedGraph:
    def invoke(self, payload, config):
        raise AssertionError("The LangGraph react agent should not run for a resume call.")


class _ScriptedFakePopen:
    responses: list[dict] = []
    calls: list[dict] = []

    def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
        input_path = Path(cmd[-3])
        output_path = Path(cmd[-1])
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        type(self).calls.append(payload)
        progress_path = Path(payload["progress_jsonl"])
        progress_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": payload["run_id"],
                    "request_id": payload["request_id"],
                    "sequence": len(type(self).calls),
                    "event_type": "phase",
                    "phase": "working",
                    "human_summary": "Working.",
                    "occurred_at": "2026-07-26T00:00:00+00:00",
                    "metadata": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output_path.write_text(
            json.dumps(type(self).responses[len(type(self).calls) - 1]), encoding="utf-8"
        )
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        return ("", "")


def _spec(tmp_path: Path, *, accepted_context=None, required_context=None) -> AgentSpec:
    input_contract = {}
    if accepted_context is not None:
        input_contract["accepted_context"] = accepted_context
    if required_context is not None:
        input_contract["required_context"] = required_context
    return AgentSpec(
        id=SPECIALIST_ID,
        name="Project Aware Agent",
        purpose="Primary responsibility: Do project-scoped work.",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-project-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
        input_contract=input_contract,
        interaction_contract={
            "progress": False,
            "clarification": True,
            "approval": False,
            "resume": True,
            "cancellation": True,
        },
    )


def _project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_dispatch_sends_canonical_project_context_when_selected(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [{"status": "success", "summary": "Done."}]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))
    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Do it"})

    call = _ScriptedFakePopen.calls[0]
    assert call["project_root"] == str(project.resolve())
    assert call["project_id"] == str(project.resolve())
    assert call["project_contract_version"] == 1
    assert "project_fingerprint" in call


def test_dispatch_omits_project_context_for_a_specialist_that_does_not_accept_it(
    monkeypatch, tmp_path
):
    spec = _spec(tmp_path, accepted_context=["references"])
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [{"status": "success", "summary": "Done."}]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))
    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Do it"})

    call = _ScriptedFakePopen.calls[0]
    assert "project_root" not in call
    assert "project_id" not in call


def test_dispatch_stops_when_selected_project_root_no_longer_exists(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))

    import shutil

    shutil.rmtree(project)

    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        reply = tool.invoke({"task": "Do it"})

    assert "no longer exists" in reply
    assert _ScriptedFakePopen.calls == []
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_FAILED


def test_dispatch_ignores_stale_project_for_a_specialist_that_does_not_accept_it(
    monkeypatch, tmp_path
):
    """A stale/invalid *selected* project should not block a specialist that
    never asked for project context in the first place."""
    spec = _spec(tmp_path, accepted_context=["references"])
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [{"status": "success", "summary": "Done."}]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))

    import shutil

    shutil.rmtree(project)

    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        reply = tool.invoke({"task": "Do it"})

    assert "Done." in reply
    assert len(_ScriptedFakePopen.calls) == 1


def test_resumed_dispatch_replays_pinned_project_context(monkeypatch, tmp_path):
    spec = _spec(tmp_path)
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": "tok-1"},
        {"status": "success", "summary": "Done."},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))
    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Do it"})

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_WAITING_CLARIFICATION

    # Selection changes (or goes stale) while the task is paused — the
    # resume must still use the context captured at the original dispatch.
    orchestrator.clear_current_project()

    orchestrator.provide_clarification("Square, please.")

    assert len(_ScriptedFakePopen.calls) == 2
    resume_call = _ScriptedFakePopen.calls[1]
    assert resume_call["project_root"] == str(project.resolve())
    assert resume_call["project_id"] == str(project.resolve())


def test_approval_resume_replays_pinned_project_context(monkeypatch, tmp_path):
    """approve_pending must pin/replay the same project context (and
    references) captured at the original dispatch as clarification/decision
    resume already do — an operator changing /project while a task sits
    waiting for approval must not silently redirect the approved run."""
    spec = _spec(tmp_path)
    project = _project_dir(tmp_path)
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [
        {
            "status": "approval_required",
            "summary": "Delete files?",
            "approval_token": "approve-1",
        },
        {"status": "success", "summary": "Deleted."},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _UnusedGraph()
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project))
    run = get_task_run_store().create_run(session_id=orchestrator.session_id, user_message="Do it")
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Do it", "references": ["spec://ref-1"]})

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_WAITING_APPROVAL

    # Selection changes while the task waits for approval — the resumed
    # dispatch must still use the context captured at the original dispatch.
    orchestrator.clear_current_project()

    reply = orchestrator.approve_pending()

    assert "Deleted." in reply
    assert len(_ScriptedFakePopen.calls) == 2
    resume_call = _ScriptedFakePopen.calls[1]
    assert resume_call["project_root"] == str(project.resolve())
    assert resume_call["project_id"] == str(project.resolve())
    assert resume_call["references"] == ["spec://ref-1"]
