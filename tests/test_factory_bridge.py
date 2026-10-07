from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import agent_hub.orchestrator as orchestrator_module
from agent_hub import factory_bridge
from agent_hub.orchestrator import _dispatch_factory_brain
from agent_hub.registry import AgentSpec
from agent_hub.task_runs import active_task_run, get_task_run_store


class _FakeProcess:
    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        input_file = Path(command[-2])
        output_file = Path(command[-1])
        payload = json.loads(input_file.read_text(encoding="utf-8"))
        run_id = payload["run_id"]
        request_id = payload["request_id"]
        events = [
            {
                "schema_version": 1,
                "run_id": run_id,
                "request_id": request_id,
                "sequence": 1,
                "event_type": "start",
                "phase": "starting",
                "human_summary": "Agent Factory accepted the task.",
                "occurred_at": "2026-07-23T01:00:00Z",
                "metadata": {},
            },
            {
                "schema_version": 1,
                "run_id": run_id,
                "request_id": request_id,
                "sequence": 2,
                "event_type": "phase",
                "phase": "design",
                "human_summary": "Agent Factory is designing the agent package.",
                "occurred_at": "2026-07-23T01:00:01Z",
                "metadata": {"source": "deep_agent"},
            },
            {
                "schema_version": 1,
                "run_id": run_id,
                "request_id": request_id,
                "sequence": 3,
                "event_type": "completed",
                "phase": "completed",
                "human_summary": "Agent Factory completed the task.",
                "occurred_at": "2026-07-23T01:00:02Z",
                "metadata": {},
            },
        ]
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO("human-readable factory log\n")
        self.returncode = 0
        self.pid = 12345
        output_file.write_text(
            json.dumps(
                {
                    "response": "Factory result",
                    "interrupted": False,
                    "status": "success",
                    "summary": "Factory result",
                    "next_task": {
                        "task_kind": "coding_task",
                        "task": "Implement the staged package.",
                        "references": [
                            "staging/agents/example-agent/BUILD_TASK.json",
                        ],
                    },
                    "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
                }
            ),
            encoding="utf-8",
        )

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode

    def poll(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_factory_bridge_streams_progress_and_preserves_final_result(monkeypatch) -> None:
    captured_payload: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        input_file = Path(command[-2])
        captured_payload.update(json.loads(input_file.read_text(encoding="utf-8")))
        return _FakeProcess(command, **kwargs)

    monkeypatch.setattr(factory_bridge.subprocess, "Popen", fake_popen)
    store = get_task_run_store()
    run = store.create_run(session_id="telegram:42", user_message="Create an agent")
    updates = []

    with active_task_run(run.id, progress_callback=updates.append):
        result = factory_bridge.invoke_factory_request(
            working_directory="/tmp/agent-factory",
            request="Create an agent",
            thread_id="hub-factory-thread-1",
            governed_skills=[
                {
                    "slug": "agent-design",
                    "version": 1,
                    "title": "Agent design",
                    "content": "Prefer bounded workflows.",
                }
            ],
        )

    assert result["response"] == "Factory result"
    assert result["interrupted"] is False
    assert result["status"] == "success"
    assert result["summary"] == "Factory result"
    assert result["next_task"]["task_kind"] == "coding_task"
    assert result["artifact_reference"] == (
        "staging/agents/example-agent/BUILD_TASK.json"
    )
    assert captured_payload["run_id"] == run.id
    assert captured_payload["request_id"]
    assert captured_payload["action"] == "invoke"
    assert captured_payload["governed_skills"][0]["slug"] == "agent-design"
    assert captured_payload["governed_skills"][0]["version"] == 1

    persisted = store.list_progress_events(run.id)
    accepted = [event for event in persisted if event.validation_status == "accepted"]
    assert [event.event_type for event in accepted] == [
        "start",
        "start",
        "phase",
        "completed",
    ]
    assert [event.sequence for event in accepted[1:]] == [1, 2, 3]
    assert accepted[-1].phase == "completed"
    assert [update.event_type for update in updates] == [
        "start",
        "start",
        "phase",
    ]
    # Hub intentionally does not notify the terminal progress event separately;
    # the unchanged final result is returned through the existing bridge contract.


@pytest.mark.parametrize("action", ["resume", "reject"])
def test_factory_bridge_resume_and_reject_preserve_structured_result(monkeypatch, action):
    captured_payload: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        input_file = Path(command[-2])
        captured_payload.update(json.loads(input_file.read_text(encoding="utf-8")))
        return _FakeProcess(command, **kwargs)

    monkeypatch.setattr(factory_bridge.subprocess, "Popen", fake_popen)
    store = get_task_run_store()
    run = store.create_run(session_id="telegram:42", user_message="Create an agent")
    with active_task_run(run.id):
        if action == "resume":
            result = factory_bridge.resume_factory_request(
                working_directory="/tmp/agent-factory",
                thread_id="hub-factory-thread-1",
            )
        else:
            result = factory_bridge.reject_factory_request(
                working_directory="/tmp/agent-factory",
                thread_id="hub-factory-thread-1",
                reason="Not approved",
            )

    assert captured_payload["action"] == action
    assert result["status"] == "success"
    assert result["summary"] == "Factory result"
    assert result["next_task"]["task_kind"] == "coding_task"
    assert result["artifact_reference"].endswith("/BUILD_TASK.json")


@pytest.mark.parametrize(
    ("factory_status", "interrupted", "hub_status"),
    [("success", False, "success"), ("waiting_approval", True, "approval_required")],
)
def test_factory_dispatch_stores_structured_result_and_maps_interruption(
    monkeypatch, factory_status, interrupted, hub_status
):
    spec = AgentSpec(
        id="agent-factory",
        name="Agent Factory",
        purpose="Design and govern specialist packages.",
        runtime={
            "mode": "factory_brain",
            "working_directory": "/tmp/agent-factory",
        },
    )
    structured = {
        "status": factory_status,
        "summary": "Factory summary",
    }
    if factory_status == "success":
        structured.update(
            {
                "next_task": {
                    "task_kind": "coding_task",
                    "task": "Implement the package.",
                    "references": ["staging/agents/example-agent/BUILD_TASK.json"],
                },
                "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
            }
        )
    monkeypatch.setattr(
        orchestrator_module,
        "invoke_factory_request",
        lambda **_kwargs: {"response": "Factory summary", "interrupted": interrupted, **structured},
    )
    monkeypatch.setattr(
        orchestrator_module,
        "get_manifest_cache",
        lambda: type("Cache", (), {"get_or_refresh": lambda self, _spec: None})(),
    )
    store = get_task_run_store()
    run = store.create_run(session_id="telegram:42", user_message="Create an agent")
    with active_task_run(run.id):
        output = _dispatch_factory_brain(spec, "Create an agent", thread_id="factory-thread")

    assert output["status"] == hub_status
    assert output["summary"] == "Factory summary"
    assert output["factory_result"]["status"] == factory_status
    recorded = store.get_run(run.id)
    assert recorded is not None
    assert recorded.raw_result["status"] == hub_status
    if factory_status == "success":
        assert recorded.raw_result["next_task"] == structured["next_task"]
        assert recorded.raw_result["artifact_reference"] == structured["artifact_reference"]
    else:
        assert recorded.state == "waiting_approval"
