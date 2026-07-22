"""Integration test for the subprocess dispatch contract.

Runs agent_hub.orchestrator._dispatch_subprocess against a real child
process (tests/fixtures/stub_specialist.py) instead of a mocked
subprocess.Popen. This exercises the parts unit tests skip over:
real argv construction, real cwd, and real cross-process JSON I/O —
the actual seam between Hub and any specialist repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_hub.orchestrator import _dispatch_subprocess
from agent_hub.registry import AgentSpec
from agent_hub.task_control import get_task_control_registry
from agent_hub.task_runs import (
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    active_task_run,
    get_task_run_store,
)

_STUB_PATH = Path(__file__).parent / "fixtures" / "stub_specialist.py"


def _make_spec(tmp_path) -> AgentSpec:
    return AgentSpec(
        id="stub-specialist",
        name="Stub Specialist",
        purpose="Contract test double",
        runtime={
            "mode": "subprocess",
            "entrypoint": f"{sys.executable} {_STUB_PATH}",
            "working_directory": str(tmp_path),
            "input_arg": "--input-json",
            "output_arg": "--output-json",
            "default_execution_mode": "execute",
        },
    )


def _run(spec, task):
    run = get_task_run_store().create_run(session_id="session-1", user_message=task)
    get_task_control_registry().register_run(run.id)
    try:
        with active_task_run(run.id):
            output = _dispatch_subprocess(spec, task)
    finally:
        get_task_control_registry().unregister_run(run.id)
    return run, output


def test_dispatch_subprocess_default_success(tmp_path):
    spec = _make_spec(tmp_path)
    run, output = _run(spec, "Do the thing")

    assert output["status"] == "success"
    assert output["result_kind"] == "execution_result"

    # A single specialist success doesn't finalize the task run — only the
    # outer HubOrchestrator.invoke() loop marks TASK_STATE_SUCCEEDED once the
    # whole graph run completes. Direct dispatch just leaves it in_progress.
    updated = get_task_run_store().get_run(run.id)
    assert updated.state == TASK_STATE_IN_PROGRESS
    assert updated.raw_result["caller_action"] == "consume_result"


def test_dispatch_subprocess_needs_clarification(tmp_path):
    spec = _make_spec(tmp_path)
    _, output = _run(spec, "SCENARIO:needs_clarification do the thing")

    assert output["status"] == "needs_clarification"
    assert output["caller_action"] == "provide_clarification"
    assert output["resume_supported"] is True
    assert output["resume_fields"] == ["request_id", "task"]


def test_dispatch_subprocess_approval_then_resume(tmp_path):
    spec = _make_spec(tmp_path)
    run, first = _run(spec, "SCENARIO:approval_required delete files")

    assert first["status"] == "approval_required"
    assert first["approval_token"] == "stub-approval-token"

    with active_task_run(run.id):
        second = _dispatch_subprocess(
            spec,
            "SCENARIO:approval_required delete files",
            human_approved=True,
            approval_token=first["approval_token"],
        )

    assert second["status"] == "success"


def test_dispatch_subprocess_failed(tmp_path):
    spec = _make_spec(tmp_path)
    run, output = _run(spec, "SCENARIO:failed do the thing")

    assert output["status"] == "failed"
    assert output["caller_action"] == "inspect_failure"

    updated = get_task_run_store().get_run(run.id)
    assert updated.state == TASK_STATE_FAILED
