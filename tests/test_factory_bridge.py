from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from agent_hub import factory_bridge
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
            json.dumps({"response": "Factory result", "interrupted": False}),
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
        )

    assert result == {"response": "Factory result", "interrupted": False}
    assert captured_payload["run_id"] == run.id
    assert captured_payload["request_id"]
    assert captured_payload["action"] == "invoke"

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
