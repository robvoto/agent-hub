"""AGENT-HUB-053: the specialist protocol is the authority for approval pauses.

An AI Tech Lead shaped approval arrives as ``status=waiting_decision`` with a
``pending_decision`` option list — ATL's manifest never advertises
``approval_required`` (see ``ai_tech_lead/agent_manifest.py``) and its approval
interrupt offers ``approve`` / ``request_changes`` / ``ask_question`` /
``cancel`` (``ai_tech_lead/agent_task_runner.py``
``_DECISION_OPTIONS_BY_KIND["approval"]``). Hub must therefore:

* refuse ``/approve`` and ``/reject`` for that pause — those commands act only
  on a true ``waiting_approval`` run — and
* resume it through the generic decision path using the specialist's own
  reported option name or number.

Approval semantics must never be inferred from prompt wording or a specialist's
internal ``pending_decision.kind``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_hub.orchestrator import HubOrchestrator
from agent_hub.registry import AgentSpec
from agent_hub.task_runs import (
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    get_task_run_store,
)

ATL_APPROVAL_OPTIONS = [
    {"name": "approve"},
    {"name": "request_changes", "needs_text": True},
    {"name": "ask_question", "needs_text": True},
    {"name": "cancel"},
]


def _orchestrator(monkeypatch) -> HubOrchestrator:
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: object()
    )
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    return HubOrchestrator(model="test")


def _atl_approval_pause() -> SimpleNamespace:
    # The exact shape AI Tech Lead publishes for an approval interrupt.
    return SimpleNamespace(
        id="run-ah053",
        state="waiting_decision",
        selected_agent_id="ai-tech-lead",
        context={
            "specialist_pending_decision": {
                "kind": "approval",
                "prompt": "Approve deleting the unused backlog loader module?",
                "options": ATL_APPROVAL_OPTIONS,
            }
        },
    )


def test_approve_command_does_not_resume_a_specialist_decision_pause(monkeypatch):
    orch = _orchestrator(monkeypatch)
    monkeypatch.setattr(orch, "pending_run", lambda: _atl_approval_pause())

    assert orch.approve_pending() == "No task is currently waiting for approval."


def test_reject_command_does_not_resume_a_specialist_decision_pause(monkeypatch):
    orch = _orchestrator(monkeypatch)
    monkeypatch.setattr(orch, "pending_run", lambda: _atl_approval_pause())

    assert orch.reject_pending("no thanks") == "No task is currently waiting for approval."


def test_reported_option_name_resumes_a_specialist_decision_pause(monkeypatch):
    orch = _orchestrator(monkeypatch)
    monkeypatch.setattr(orch, "pending_run", lambda: _atl_approval_pause())
    captured: dict = {}

    def _provide(option, text="", *, actor="human", progress_notify=None):
        captured.update(option=option, text=text, actor=actor)
        return "resumed"

    monkeypatch.setattr(orch, "provide_decision", _provide)

    # "approve" and "cancel" are ATL-reported option names; a normal reply
    # carries them straight through the decision path.
    assert orch.provide_decision_reply("approve") == "resumed"
    assert captured == {"option": "approve", "text": "", "actor": "human"}

    assert orch.provide_decision_reply("cancel") == "resumed"
    assert captured == {"option": "cancel", "text": "", "actor": "human"}


def test_reported_option_number_resumes_a_specialist_decision_pause(monkeypatch):
    orch = _orchestrator(monkeypatch)
    monkeypatch.setattr(orch, "pending_run", lambda: _atl_approval_pause())
    captured: dict = {}

    def _provide(option, text="", *, actor="human", progress_notify=None):
        captured.update(option=option, text=text, actor=actor)
        return "resumed"

    monkeypatch.setattr(orch, "provide_decision", _provide)

    assert orch.provide_decision_reply("1") == "resumed"
    assert captured == {"option": "approve", "text": "", "actor": "human"}


def test_true_waiting_approval_still_resumes_via_approve_command(monkeypatch, tmp_path):
    """Guard: reserving /approve for true approvals must not break that path."""
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
            Path(payload["progress_jsonl"]).write_text(
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
            output_path.write_text(
                json.dumps({"status": "success", "summary": "Approved task completed."}),
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
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self, registry=None, **kwargs: object()
    )
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
    assert calls[0]["request_id"] == "req-1"
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None and updated.state == TASK_STATE_SUCCEEDED
