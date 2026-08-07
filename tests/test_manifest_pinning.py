"""AGENT-HUB-040: in-flight tasks are pinned to the manifest they were
dispatched against, so a specialist that Factory changes or removes from the
registry while a task is paused does not silently change resume behavior
(or fail just because the id moved)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from agent_hub.orchestrator import HubOrchestrator, _make_agent_tool
from agent_hub.registry import AgentSpec, spec_fingerprint
from agent_hub.task_runs import (
    TASK_STATE_WAITING_CLARIFICATION,
    active_task_run,
    get_task_run_store,
)

SPECIALIST_ID = "pinned-agent"


class _UnusedGraph:
    def invoke(self, payload, config):
        raise AssertionError("The LangGraph react agent should not run for a resume call.")


class _ScriptedFakePopen:
    responses: list[dict] = []
    calls: list[dict] = []
    cwds: list[str] = []

    def __init__(self, cmd, cwd, stdout, stderr, text, **kwargs):
        input_path = Path(cmd[-3])
        output_path = Path(cmd[-1])
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        type(self).calls.append(payload)
        type(self).cwds.append(cwd)
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


def _spec(
    workdir: Path, *, version: str, purpose: str, resume: bool = False
) -> AgentSpec:
    kwargs: dict = dict(
        id=SPECIALIST_ID,
        name="Pinned Agent",
        purpose=purpose,
        version=version,
        runtime={
            "mode": "subprocess",
            "entrypoint": "fake-pinned-agent",
            "working_directory": str(workdir),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )
    if resume:
        kwargs["interaction_contract"] = {
            "progress": False,
            "clarification": True,
            "approval": False,
            "resume": True,
            "cancellation": True,
        }
    return AgentSpec(**kwargs)


def _dispatch_and_pause(monkeypatch, spec):
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?"},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge it"
    )
    tool = _make_agent_tool(spec)
    with active_task_run(run.id):
        tool.invoke({"task": "Forge it"})
    return orchestrator, run


def test_dispatch_pins_the_spec_used_into_task_context(monkeypatch, tmp_path):
    spec = _spec(tmp_path, version="1.0.0", purpose="Primary responsibility: Forge widgets.")
    _orchestrator, run = _dispatch_and_pause(monkeypatch, spec)

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_WAITING_CLARIFICATION
    assert updated.context["pinned_agent_version"] == "1.0.0"
    assert updated.context["pinned_agent_fingerprint"] == spec_fingerprint(spec)
    assert updated.context["pinned_agent_spec"] == dataclasses.asdict(spec)


def test_require_spec_prefers_pinned_manifest_when_live_registry_changed(monkeypatch, tmp_path):
    original = _spec(tmp_path, version="1.0.0", purpose="Primary responsibility: Forge widgets.")
    orchestrator, run = _dispatch_and_pause(monkeypatch, original)

    changed = _spec(tmp_path, version="2.0.0", purpose="Primary responsibility: Forge gadgets.")
    orchestrator._registry = [changed]

    pending = get_task_run_store().get_run(run.id)
    resolved = orchestrator._require_spec(pending)

    assert resolved.version == "1.0.0"
    assert resolved.purpose == "Primary responsibility: Forge widgets."


def test_require_spec_prefers_pinned_manifest_when_agent_removed(monkeypatch, tmp_path):
    original = _spec(tmp_path, version="1.0.0", purpose="Primary responsibility: Forge widgets.")
    orchestrator, run = _dispatch_and_pause(monkeypatch, original)

    orchestrator._registry = []

    pending = get_task_run_store().get_run(run.id)
    resolved = orchestrator._require_spec(pending)

    assert resolved.id == SPECIALIST_ID
    assert resolved.version == "1.0.0"


def test_require_spec_falls_back_to_live_registry_without_a_pin(monkeypatch, tmp_path):
    """A paused run created before manifest pinning existed has no
    `pinned_agent_spec` in its context — resume must still work by falling
    back to a live-registry lookup by id."""
    spec = _spec(tmp_path, version="1.0.0", purpose="Primary responsibility: Forge widgets.")
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge it"
    )
    get_task_run_store().transition(
        run.id,
        TASK_STATE_WAITING_CLARIFICATION,
        selected_agent_id=spec.id,
        dispatched_task="Forge it",
    )

    pending = get_task_run_store().get_run(run.id)
    resolved = orchestrator._require_spec(pending)
    assert resolved is spec


def test_refresh_registry_reports_added_changed_removed_and_invalid(monkeypatch, tmp_path):
    from agent_hub.registry import RegistryLoadError

    def _spec2(agent_id: str, purpose: str) -> AgentSpec:
        return AgentSpec(
            id=agent_id, name=agent_id, purpose=purpose, runtime={"mode": "subprocess"}
        )

    kept = _spec2("kept-agent", "Stays the same")
    stale = _spec2("changed-agent", "Old purpose")
    removed = _spec2("removed-agent", "Goes away")
    monkeypatch.setattr(
        "agent_hub.orchestrator._load_specialists", lambda: [kept, stale, removed]
    )
    monkeypatch.setattr("agent_hub.orchestrator._load_registry_errors", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()

    fresh = _spec2("changed-agent", "New purpose")
    added = _spec2("added-agent", "Just showed up")
    monkeypatch.setattr(
        "agent_hub.orchestrator._load_specialists", lambda: [kept, fresh, added]
    )
    monkeypatch.setattr(
        "agent_hub.orchestrator._load_registry_errors",
        lambda: [RegistryLoadError(source="bad-agent/agent.json", message="invalid JSON")],
    )

    report = orchestrator.refresh_registry()

    assert "3 agent(s) active" in report
    assert "Added: added-agent" in report
    assert "Changed: changed-agent" in report
    assert "Removed: removed-agent" in report
    assert "Invalid manifest(s) (1)" in report
    assert "bad-agent/agent.json: invalid JSON" in report


def test_refresh_registry_reports_no_changes_and_no_invalid_manifests(monkeypatch):
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr("agent_hub.orchestrator._load_registry_errors", lambda: [])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    report = orchestrator.refresh_registry()

    assert "0 agent(s) active" in report
    assert "Added: none" in report
    assert "Changed: none" in report
    assert "Removed: none" in report
    assert "Invalid manifests: none." in report


def test_agents_status_reports_current_agents_without_reloading(monkeypatch, tmp_path):
    spec = _spec(tmp_path, version="1.0.0", purpose="Primary responsibility: Forge widgets.")
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [spec])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()

    # A later registry change on disk must NOT be reflected by /agents-status
    # — it only reports state as of the last reconciliation, unlike
    # /agents-refresh, which re-reads first.
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])

    report = orchestrator.agents_status()

    assert "1 agent(s) active" in report
    assert f"{SPECIALIST_ID} (v1.0.0, fingerprint {spec_fingerprint(spec)[:8]})" in report
    assert "Invalid manifests: none." in report


def test_agents_status_reports_invalid_manifests_from_last_refresh(monkeypatch):
    from agent_hub.registry import RegistryLoadError

    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [])
    monkeypatch.setattr(
        "agent_hub.orchestrator._load_registry_errors",
        lambda: [RegistryLoadError(source="bad-agent/agent.json", message="invalid JSON")],
    )
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    report = orchestrator.agents_status()

    assert "0 agent(s) active" in report
    assert "  (none)" in report
    assert "Invalid manifest(s) (1)" in report
    assert "bad-agent/agent.json: invalid JSON" in report


def test_resumed_dispatch_uses_pinned_runtime_after_agent_changed_in_registry(
    monkeypatch, tmp_path
):
    """End-to-end: the *subprocess itself* is invoked from the original
    working directory on resume, even though the live registry now points
    the same agent id at a different one — proving the pin governs actual
    dispatch, not just bookkeeping fields."""
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    v2_dir = tmp_path / "v2"
    v2_dir.mkdir()

    original = _spec(
        v1_dir, version="1.0.0", purpose="Primary responsibility: Forge widgets.", resume=True
    )
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.cwds = []
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": "tok-1"},
        {"status": "success", "summary": "Forged."},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [original])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge it"
    )
    tool = _make_agent_tool(original)
    with active_task_run(run.id):
        tool.invoke({"task": "Forge it"})

    # Factory changes the agent while the task is paused: same id, new
    # version and purpose, pointing at a different working directory.
    changed = _spec(
        v2_dir, version="2.0.0", purpose="Primary responsibility: Forge gadgets.", resume=True
    )
    orchestrator._registry = [changed]

    reply = orchestrator.provide_clarification("Square, please.")

    assert "Forged." in reply
    assert len(_ScriptedFakePopen.cwds) == 2
    assert _ScriptedFakePopen.cwds[1] == str(v1_dir)

    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.context["pinned_agent_version"] == "1.0.0"


def test_resumed_dispatch_uses_pinned_runtime_after_agent_removed_from_registry(
    monkeypatch, tmp_path
):
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir()
    original = _spec(
        v1_dir, version="1.0.0", purpose="Primary responsibility: Forge widgets.", resume=True
    )
    _ScriptedFakePopen.calls = []
    _ScriptedFakePopen.cwds = []
    _ScriptedFakePopen.responses = [
        {"status": "needs_clarification", "summary": "Which shape?", "resume_token": "tok-1"},
        {"status": "success", "summary": "Forged."},
    ]
    monkeypatch.setattr("agent_hub.orchestrator.subprocess.Popen", _ScriptedFakePopen)
    monkeypatch.setattr("agent_hub.orchestrator._load_specialists", lambda: [original])
    monkeypatch.setattr(HubOrchestrator, "_build_graph", lambda self, registry=None: _UnusedGraph())

    orchestrator = HubOrchestrator()
    run = get_task_run_store().create_run(
        session_id=orchestrator.session_id, user_message="Forge it"
    )
    tool = _make_agent_tool(original)
    with active_task_run(run.id):
        tool.invoke({"task": "Forge it"})

    orchestrator._registry = []  # Factory removed the agent entirely.

    reply = orchestrator.provide_clarification("Square, please.")

    assert "Forged." in reply
    assert len(_ScriptedFakePopen.cwds) == 2
    assert _ScriptedFakePopen.cwds[1] == str(v1_dir)
