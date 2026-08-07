"""True specialist clarification resume vs. the legacy reconstructed-task fallback.

A specialist that declares `interaction_contract.resume = true` and returns an
opaque `resume_token` when it asks for clarification gets a fundamentally
different resume dispatch than a legacy specialist: Hub replays the same
request/run identity, the clarification reply as `task` (not concatenated),
the opaque token verbatim via the envelope's `resume` field, and the
*original* universal context captured at first dispatch — never re-derived
live. A specialist that never declares `resume` keeps exactly today's
behavior: the reply is concatenated onto the original task and redispatched
as a fresh instruction.

None of this requires orchestrator.py to know the fake specialist's id —
that absence is itself part of the proof.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import AgentSpec
from agent_hub.task_runs import (
    TASK_STATE_FAILED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    active_task_run,
    get_task_run_store,
)

RESUME_SPECIALIST_ID = "checkpoint-agent"
LEGACY_SPECIALIST_ID = "legacy-agent"


class _UnusedGraph:
    """Stand-in graph — resume calls never invoke the LangGraph react agent."""

    def invoke(self, payload, config):
        raise AssertionError("The LangGraph react agent should not run for a resume call.")


class _ScriptedFakePopen:
    """Fake subprocess that returns one scripted response per dispatch call."""

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
                    "occurred_at": "2026-07-24T00:00:00+00:00",
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


def _resume_capable_spec(tmp_path: Path) -> AgentSpec:
    return AgentSpec(
        id=RESUME_SPECIALIST_ID,
        name="Checkpoint Agent",
        purpose="Primary responsibility: Do checkpointed work.",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-checkpoint-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
        interaction_contract={
            "progress": False,
            "clarification": True,
            "approval": True,
            "resume": True,
            "cancellation": True,
        },
    )


def _legacy_spec(tmp_path: Path) -> AgentSpec:
    return AgentSpec(
        id=LEGACY_SPECIALIST_ID,
        name="Legacy Agent",
        purpose="Primary responsibility: Do legacy work.",
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-legacy-agent",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
        # No interaction_contract at all — the pre-existing default for
        # every specialist registered before this feature existed.
    )


def _dispatch_initial_task(monkeypatch, spec, task_text, references=None):
    """Build an orchestrator, dispatch the initial task, and return it plus the run."""
    _ScriptedFakePopen.calls = []
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message=task_text
    )
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        payload = {"task": task_text}
        if references:
            payload["references"] = references
        tool.invoke(payload)
    return orchestrator, run


def test_true_resume_preserves_identity_and_transports_opaque_token_unchanged(
    monkeypatch, tmp_path
):
    spec = _resume_capable_spec(tmp_path)
    resume_token = {"checkpoint": "thread-42", "step": 3}
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": resume_token},
        {"status": "success", "summary": "Done."},
    ]

    orchestrator, run = _dispatch_initial_task(monkeypatch, spec, "Forge something")
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_WAITING_CLARIFICATION

    reply = orchestrator.provide_clarification("Square, please.")

    assert "Done." in reply
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_SUCCEEDED

    first_call, resume_call = _ScriptedFakePopen.calls
    assert resume_call["request_id"] == first_call["request_id"]
    assert resume_call["run_id"] == first_call["run_id"]
    # The clarification reply is sent as the task itself — not concatenated
    # onto the original.
    assert resume_call["task"] == "Square, please."
    # The opaque token is transported byte-for-byte, unexamined.
    assert resume_call["resume"] == resume_token


def test_original_universal_context_survives_the_pause(monkeypatch, tmp_path):
    """Prove the true-resume dispatch replays the project context captured at
    the *original* dispatch rather than re-resolving the operator's live
    /project selection. `_resolve_project_context_for_task_run` is stubbed
    to return a different project on each call — if the resume path called
    it again (instead of replaying the captured override), it would see
    "drifted-project" instead of the original."""
    from agent_hub.project_context import ProjectContext, ProjectContextResolution

    spec = _resume_capable_spec(tmp_path)

    live_lookups = [
        ProjectContextResolution(
            context=ProjectContext(
                project_id="original-project",
                root="original-project",
                contract_version=1,
                fingerprint="fp-original",
                metadata={},
            ),
            error=None,
        ),
        ProjectContextResolution(
            context=ProjectContext(
                project_id="drifted-project",
                root="drifted-project",
                contract_version=1,
                fingerprint="fp-drifted",
                metadata={},
            ),
            error=None,
        ),
    ]
    live_lookup_calls: list[str] = []

    def _fake_resolve_project_context(task_run_id):
        resolution = live_lookups[len(live_lookup_calls)]
        live_lookup_calls.append(resolution.context.project_id)
        return resolution

    monkeypatch.setattr(
        "agent_hub.orchestrator._resolve_project_context_for_task_run",
        _fake_resolve_project_context,
    )
    _ScriptedFakePopen.calls = []
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge something"
    )
    resume_token = "thread-99"
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": resume_token},
        {"status": "success", "summary": "Done."},
    ]
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Forge something", "references": ["spec://ref-1"]})

    orchestrator.provide_clarification("Square, please.")

    first_call, resume_call = _ScriptedFakePopen.calls
    assert first_call["project_root"] == "original-project"
    assert resume_call["project_root"] == "original-project"
    assert resume_call["references"] == ["spec://ref-1"]
    # The live lookup fired once (for the original dispatch only) — the
    # resume dispatch replayed the captured value instead of calling it
    # again, so it never saw "drifted-project".
    assert live_lookup_calls == ["original-project"]


def test_legacy_specialist_keeps_reconstructed_task_fallback(monkeypatch, tmp_path):
    spec = _legacy_spec(tmp_path)
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?"},
        {"status": "success", "summary": "Done."},
    ]

    orchestrator, run = _dispatch_initial_task(monkeypatch, spec, "Do the legacy thing")
    orchestrator.provide_clarification("Square, please.")

    first_call, resume_call = _ScriptedFakePopen.calls
    assert resume_call["task"] == (
        "Do the legacy thing\n\nAdditional clarification from the user: Square, please."
    )
    assert "resume" not in resume_call


def test_missing_resume_token_fails_clearly_instead_of_guessing(monkeypatch, tmp_path):
    spec = _resume_capable_spec(tmp_path)
    _ScriptedFakePopen.responses = [
        # Contract violation: resume=true declared, but no resume_token returned.
        {"status": "needs_clarification", "summary": "Which shape?"},
    ]

    orchestrator, run = _dispatch_initial_task(monkeypatch, spec, "Forge something")

    reply = orchestrator.provide_clarification("Square, please.")

    assert "Cannot resume" in reply
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_FAILED
    # No second dispatch was attempted.
    assert len(_ScriptedFakePopen.calls) == 1


def test_oversized_resume_token_is_treated_as_missing_and_fails_clearly(monkeypatch, tmp_path):
    spec = _resume_capable_spec(tmp_path)
    oversized_token = "x" * 20_000
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": oversized_token},
    ]

    orchestrator, run = _dispatch_initial_task(monkeypatch, spec, "Forge something")

    reply = orchestrator.provide_clarification("Square, please.")

    assert "Cannot resume" in reply
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_FAILED
    assert len(_ScriptedFakePopen.calls) == 1


def test_approval_resume_still_works_unaffected_by_clarification_changes(monkeypatch, tmp_path):
    spec = _resume_capable_spec(tmp_path)
    _ScriptedFakePopen.responses = [
        {
            "status": "approval_required",
            "summary": "Confirm before proceeding.",
            "approval_token": "tok-1",
        },
        {"status": "success", "summary": "Done."},
    ]

    orchestrator, run = _dispatch_initial_task(monkeypatch, spec, "Forge something")
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_WAITING_APPROVAL

    reply = orchestrator.approve_pending()

    assert "Done." in reply
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_SUCCEEDED
    first_call, resume_call = _ScriptedFakePopen.calls
    assert resume_call["request_id"] == first_call["request_id"]
    assert resume_call["human_approved"] is True
    assert resume_call["approval_token"] == "tok-1"
    # Approval resume is untouched by this feature: it still sends the
    # original task, not a reconstruction, and has no "resume" field.
    assert resume_call["task"] == "Forge something"
    assert "resume" not in resume_call
