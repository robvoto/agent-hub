"""Final integration test (AGENT-HUB-040): the full discovery lifecycle plus
a realistic specialist conversation, driven entirely through Hub's generic
machinery — no code here is specific to any one specialist's identity.

Two scenarios:

1. Factory adds a fake specialist package to the enabled registry, changes
   its purpose, then disables it, all while a single long-lived
   HubOrchestrator keeps running. Hub discovers, refreshes, and stops
   routing to it without a restart (registry.load_registry +
   HubOrchestrator._reconcile_registry, unmodified).

2. A specialist shaped exactly like AI Tech Lead's real, current output
   contract (see ai_tech_lead/agent_task_runner.py and that repo's
   docs/ARCHITECTURE.md: subprocess mode, accepted_context=["project_root"]
   only, a dead-end needs_clarification pause, then a
   waiting_decision/pending_decision approval pause with named options) runs
   a full back-and-forth conversation through Hub: dispatch -> clarify ->
   decide -> success.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import load_registry
from agent_hub.task_runs import (
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_CLARIFICATION,
    TASK_STATE_WAITING_DECISION,
    active_task_run,
    get_task_run_store,
)


class _FakeGraph:
    """Stand-in for the LangGraph react agent for scenarios that drive
    dispatch/resume directly and don't need real LLM tool routing."""

    def __init__(self, response: str = "ok") -> None:
        self._response = response

    def invoke(self, payload, config):
        return {"messages": [SimpleNamespace(content=self._response)]}


def _write_agent_json(agent_dir: Path, manifest: dict) -> None:
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json").write_text(json.dumps(manifest), encoding="utf-8")


def _widget_manifest(purpose: str, working_directory: Path) -> dict:
    return {
        "id": "widget-forge",
        "name": "Widget Forge",
        "purpose": purpose,
        "tools": [],
        "runtime": {
            "mode": "subprocess",
            "entrypoint": "fake-widget-forge",
            "working_directory": str(working_directory),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    }


def test_factory_add_change_disable_lifecycle_without_hub_restart(monkeypatch, tmp_path):
    registry_dir = tmp_path / "agents"
    registry_dir.mkdir()
    working_directory = tmp_path / "workdir"
    working_directory.mkdir()

    build_calls: list[list[str]] = []

    def _fake_build_graph(self, registry=None, **kwargs):
        active = self._registry if registry is None else registry
        build_calls.append([spec.id for spec in active])
        return _FakeGraph("ok")

    # `_load_specialists` here is the real generic loader pointed at a real
    # tmp_path directory — Factory "adding an agent" is a literal agent.json
    # file appearing on disk, not a Python list being edited.
    monkeypatch.setattr(
        "agent_hub.orchestrator._load_specialists", lambda: load_registry(registry_dir)
    )
    monkeypatch.setattr(HubOrchestrator, "_build_graph", _fake_build_graph)

    # 1. Nothing is registered yet — Hub starts empty.
    orchestrator = HubOrchestrator()
    assert orchestrator.registry == []
    assert build_calls == [[]]

    # 2. Factory creates and enables a fake agent package.
    _write_agent_json(
        registry_dir / "widget-forge",
        _widget_manifest(
            "Primary responsibility: Forge widgets.\n"
            "Select for: widget requests.\nDo not select for: anything else.",
            working_directory,
        ),
    )

    orchestrator.invoke("ping")
    assert [spec.id for spec in orchestrator.registry] == ["widget-forge"]
    # Reconciliation sees the newly registered agent, but because this legacy
    # fixture advertises no task_contract it is not eligible for dispatch.
    assert build_calls[-2:] == [["widget-forge"], []]

    # 3. Factory changes its purpose/workflow. Hub refreshes it and rebuilds
    # the tool bound to it — no restart.
    calls_before_change = len(build_calls)
    _write_agent_json(
        registry_dir / "widget-forge",
        _widget_manifest(
            "Primary responsibility: Forge PREMIUM widgets only.\n"
            "Select for: premium widget requests.\nDo not select for: anything else.",
            working_directory,
        ),
    )

    orchestrator.invoke("ping")
    assert orchestrator.registry[0].purpose.startswith(
        "Primary responsibility: Forge PREMIUM widgets only."
    )
    assert len(build_calls) == calls_before_change + 2
    assert build_calls[-2:] == [["widget-forge"], []]

    # 4. Factory disables it — this is how Factory's own `delete` command
    # works: the directory is removed from the enabled registry. Hub stops
    # routing to it on the very next turn.
    shutil.rmtree(registry_dir / "widget-forge")

    orchestrator.invoke("ping")
    assert orchestrator.registry == []
    assert build_calls[-1] == []


class _ScriptedFakePopen:
    """Fake subprocess that returns one scripted response per dispatch call."""

    responses: list[dict] = []
    calls: list[dict] = []

    def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
        input_path = Path(cmd[-3])
        output_path = Path(cmd[-1])
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        type(self).calls.append(payload)

        Path(payload["progress_jsonl"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": payload["run_id"],
                    "request_id": payload["request_id"],
                    "sequence": len(type(self).calls),
                    "event_type": "phase",
                    "phase": "working",
                    "human_summary": "Working on the task.",
                    "occurred_at": "2026-07-26T00:00:00+00:00",
                    "metadata": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        output = type(self).responses[len(type(self).calls) - 1]
        output_path.write_text(json.dumps(output), encoding="utf-8")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        return ("", "")


def _ai_tech_lead_shaped_manifest(working_directory: Path) -> dict:
    return {
        "id": "ai-tech-lead",
        "name": "AI Tech Lead",
        "purpose": (
            "Primary responsibility: Lead and execute work on new or existing technical "
            "solutions.\n"
            "Select for: Implementing backlog items, building new technical solutions, "
            "or changing code in a new or existing technical solution, subject only to "
            "available authorised access.\n"
            "Do not select for: Designing a new specialist agent package."
        ),
        "tools": ["run_agent_task"],
        "runtime": {
            "mode": "subprocess",
            "entrypoint": "fake-ai-tech-lead",
            "working_directory": str(working_directory),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
        "input_contract": {
            "protocol": "agent-hub.task",
            "protocol_version": 1,
            "required_fields": ["task"],
            "accepted_context": ["project_root"],
        },
        "interaction_contract": {
            "progress": True,
            "clarification": True,
            "approval": True,
            # AI Tech Lead's real runner resumes purely via request_id +
            # decision (agent_task_runner.py) — it never emits or reads a
            # resume_token, so this stays false; true-resume is a separate,
            # unrelated mechanism this specialist doesn't use.
            "resume": False,
            "cancellation": True,
        },
    }


def test_ai_tech_lead_shaped_full_back_and_forth_workflow_through_hub(monkeypatch, tmp_path):
    registry_dir = tmp_path / "agents"
    working_directory = tmp_path / "workdir"
    project_dir = tmp_path / "project"
    working_directory.mkdir()
    project_dir.mkdir()
    _write_agent_json(
        registry_dir / "ai-tech-lead", _ai_tech_lead_shaped_manifest(working_directory)
    )

    specs = load_registry(registry_dir)
    spec = specs[0]

    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [
        {
            "status": "needs_clarification",
            "summary": "Which backlog item should I use?",
        },
        {
            "status": "waiting_decision",
            "summary": "A decision is required.",
            "pending_decision": {
                "kind": "approval",
                "thread_id": "subprocess-req-1",
                "prompt": "Approve deleting the unused backlog loader module?",
                "options": [
                    {"name": "approve"},
                    {"name": "request_changes", "needs_text": True},
                    {"name": "ask_question", "needs_text": True},
                    {"name": "cancel"},
                ],
            },
        },
        {
            "status": "success",
            "summary": "Deleted the unused backlog loader module.",
            "coding_agent_instruction": "Removed src/ai_tech_lead/old_backlog_loader.py.",
        },
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: _FakeGraph("unused")
    )

    orchestrator = HubOrchestrator()
    orchestrator.set_current_project(str(project_dir))
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Clean up dead code"
    )
    # Mirrors what HubOrchestrator.invoke() itself records on every new run
    # (see orchestrator.py) — needed here since this test drives the tool
    # call directly instead of going through invoke().
    get_task_run_store().update_run(run.id, context_updates={"target_project": str(project_dir)})
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        reply = tool.invoke({"task": "Clean up dead code", "references": ["AGENT-HUB-999"]})

    # Step 1: dead-end clarification. project_root is passed (accepted);
    # references is dropped (declared accepted_context is project_root only).
    assert "Which backlog item should I use?" in reply
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_WAITING_CLARIFICATION
    first_call = _ScriptedFakePopen.calls[0]
    assert first_call["project_root"] == str(project_dir)
    assert "references" not in first_call

    # Step 2: the user answers in plain text. AI Tech Lead does not declare
    # true resume, so Hub falls back to its universal reconstructed-task
    # concatenation — the same generic path every non-resuming specialist
    # uses, not something written for AI Tech Lead specifically.
    reply = orchestrator.provide_clarification("Use AGENT-HUB-999")
    assert "Approve deleting the unused backlog loader module?" in reply
    assert "approve" in reply
    assert "request_changes (needs text)" in reply
    paused = get_task_run_store().get_run(run.id)
    assert paused.state == TASK_STATE_WAITING_DECISION
    second_call = _ScriptedFakePopen.calls[1]
    assert "Additional clarification from the user: Use AGENT-HUB-999" in second_call["task"]
    assert second_call["request_id"] == first_call["request_id"]
    assert second_call["project_root"] == str(project_dir)

    # Step 3: the user approves. Hub resubmits the same request_id with a
    # decision object built from the specialist's own last-reported options.
    reply = orchestrator.provide_decision("approve")
    assert "Deleted the unused backlog loader module." in reply
    final = get_task_run_store().get_run(run.id)
    assert final.state == TASK_STATE_SUCCEEDED
    third_call = _ScriptedFakePopen.calls[2]
    assert third_call["request_id"] == first_call["request_id"]
    assert third_call["decision"] == {"option": "approve", "text": "", "actor": "human"}
    assert third_call["project_root"] == str(project_dir)
