"""Conformance test for the universal Hub<->specialist dispatch contract.

Registers a brand-new fake specialist ("widget-forge") that has never existed
anywhere in this codebase, purely through an agent.json fixture, and proves:

  1. Hub discovers it through the unmodified generic registry loader.
  2. Hub dispatches to it through the unmodified generic envelope/state
     machine — progress, clarification, approval, and resume all work end to
     end via the same `_make_agent_tool` / `_dispatch_subprocess` /
     `HubOrchestrator` code paths every other specialist uses.
  3. A `references` pointer the caller supplies reaches the specialist
     uninterpreted.
  4. An arbitrary manifest field Hub has no defined meaning for
     ("webhook_url") survives into `AgentSpec.extensions` untouched.

Nothing in orchestrator.py, registry.py, or manifest_cache.py has (or needs)
an `if spec.id == "widget-forge"` branch for this test to pass — that absence
is the proof that adding a new conforming specialist requires no Hub code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import load_registry
from agent_hub.task_runs import (
    TASK_STATE_DISPATCHED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_RECEIVED,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    active_task_run,
    get_task_run_store,
)

FAKE_SPECIALIST_ID = "widget-forge"


def _write_fake_specialist_manifest(registry_dir: Path, working_directory: Path) -> None:
    agent_dir = registry_dir / FAKE_SPECIALIST_ID
    agent_dir.mkdir(parents=True)
    manifest = {
        "id": FAKE_SPECIALIST_ID,
        "name": "Widget Forge",
        "purpose": (
            "Primary responsibility: Forge widgets.\n"
            "Select for: Requests to forge a widget.\n"
            "Do not select for: Anything else."
        ),
        "tools": [],
        "version": "0.1.0",
        "input_contract": {
            "protocol": "agent-hub.task",
            "protocol_version": 1,
            "required_fields": ["task"],
            "optional_fields": [
                "request_id",
                "run_id",
                "source",
                "execution_mode",
                "progress_jsonl",
                "project_root",
                "references",
                "human_approved",
                "approval_token",
            ],
            "accepted_context": ["project_root", "references"],
        },
        "interaction_contract": {
            "progress": False,
            "clarification": True,
            "approval": True,
            "resume": True,
            "cancellation": True,
        },
        "runtime": {
            "mode": "subprocess",
            "entrypoint": "fake-widget-forge",
            "working_directory": str(working_directory),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
        "output_contract": {
            "status_values": [
                "success",
                "needs_clarification",
                "approval_required",
                "failed",
            ],
        },
        # An arbitrary, Hub-unaware field — proves the extension mechanism,
        # not a special case written for this test.
        "webhook_url": "https://example.invalid/hook",
    }
    (agent_dir / "agent.json").write_text(json.dumps(manifest), encoding="utf-8")


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
                    "human_summary": "Forging widgets.",
                    "occurred_at": "2026-07-23T00:00:00+00:00",
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


def test_widget_forge_discovered_and_dispatched_through_universal_envelope(
    monkeypatch, tmp_path, caplog
):
    registry_dir = tmp_path / "agents"
    working_directory = tmp_path / "workdir"
    working_directory.mkdir()
    _write_fake_specialist_manifest(registry_dir, working_directory)

    # 1. Discovery: the unmodified generic loader finds a specialist it has
    # never seen before.
    specs = load_registry(registry_dir)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.id == FAKE_SPECIALIST_ID
    assert spec.input_contract["protocol"] == "agent-hub.task"
    assert spec.interaction_contract["clarification"] is True
    assert spec.interaction_contract["approval"] is True
    # An unrecognized manifest field survives with zero Hub code written for it.
    assert spec.extensions["webhook_url"] == "https://example.invalid/hook"

    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape should the widgets be?"},
        {
            "status": "approval_required",
            "summary": "Confirm before forging 3 widgets.",
            "approval_token": "widget-approval-token",
        },
        {"status": "success", "summary": "Forged 3 widgets."},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(
        HubOrchestrator, "_build_graph", lambda self: _UnusedGraph()
    )

    # 2. Initial dispatch, with an explicit user-provided reference. The
    # orchestrator is built first so the run is tagged with its session,
    # letting the later resume calls find it as the pending run.
    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge some widgets"
    )
    tool = _make_agent_tool(spec)
    with caplog.at_level(logging.INFO, logger="agent_hub.human"), active_task_run(run.id):
        reply = tool.invoke({"task": "Forge some widgets", "references": ["spec://widget-42"]})

    # A human operator watching the log should see the reference being relayed.
    assert "Passing 1 reference(s) to Widget Forge: spec://widget-42" in caplog.text

    assert "Which shape should the widgets be?" in reply
    after_clarification_request = get_task_run_store().get_run(run.id)
    assert after_clarification_request is not None
    assert after_clarification_request.state == TASK_STATE_WAITING_CLARIFICATION

    # References are relayed uninterpreted — Hub does not parse or act on them.
    assert _ScriptedFakePopen.calls[0]["task"] == "Forge some widgets"
    assert _ScriptedFakePopen.calls[0]["references"] == ["spec://widget-42"]

    # 3. Resume after clarification, through the unmodified generic
    # orchestrator resume path — no widget-forge-specific code exists there.
    reply = orchestrator.provide_clarification("Square widgets, please.")

    assert "Confirm before forging 3 widgets." in reply
    after_approval_request = get_task_run_store().get_run(run.id)
    assert after_approval_request is not None
    assert after_approval_request.state == TASK_STATE_WAITING_APPROVAL
    assert after_approval_request.approval_token == "widget-approval-token"
    assert "Additional clarification from the user: Square widgets, please." in (
        _ScriptedFakePopen.calls[1]["task"]
    )

    # 4. Resume after approval, again through the unmodified generic path.
    reply = orchestrator.approve_pending()

    assert "Forged 3 widgets." in reply
    final = get_task_run_store().get_run(run.id)
    assert final is not None
    assert final.state == TASK_STATE_SUCCEEDED
    assert _ScriptedFakePopen.calls[2]["human_approved"] is True
    assert _ScriptedFakePopen.calls[2]["approval_token"] == "widget-approval-token"

    # The full generic state machine ran exactly as it would for any other
    # conforming specialist.
    events = get_task_run_store().list_events(run.id)
    assert [event.to_state for event in events] == [
        TASK_STATE_RECEIVED,
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_WAITING_CLARIFICATION,
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_WAITING_APPROVAL,
        TASK_STATE_ROUTED,
        TASK_STATE_DISPATCHED,
        TASK_STATE_IN_PROGRESS,
        TASK_STATE_SUCCEEDED,
    ]


class _UnusedGraph:
    """Stand-in graph — the resume paths under test never call it."""

    def invoke(self, payload, config):
        raise AssertionError("The LangGraph react agent should not run for a resume call.")
