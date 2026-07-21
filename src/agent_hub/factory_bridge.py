"""Subprocess bridge for calling Agent Factory as a specialist agent."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import textwrap
import uuid
from pathlib import Path
from typing import Any

from .config import AGENT_FACTORY_ROOT
from .log_config import get_human_logger
from .registry import AgentSpec
from .task_control import TaskCancelled, get_task_control_registry
from .task_runs import get_current_task_run_id

logger = logging.getLogger(__name__)
human_logger = get_human_logger()

_BRIDGE_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    root = Path(sys.argv[1])
    input_file = Path(sys.argv[2])
    output_file = Path(sys.argv[3])

    sys.path.insert(0, str(root / "src"))

    from agent_factory.factory_brain import (
        invoke_factory_brain,
        reject_factory_brain,
        resume_factory_brain,
    )

    payload = json.loads(input_file.read_text(encoding="utf-8"))
    action = payload["action"]
    thread_id = payload["thread_id"]
    purpose = payload.get("purpose", "coding")

    if action == "invoke":
        response, interrupted = invoke_factory_brain(
            payload["request"],
            thread_id=thread_id,
            purpose=purpose,
        )
        result = {"response": response, "interrupted": interrupted}
    elif action == "resume":
        response, interrupted = resume_factory_brain(thread_id, purpose=purpose)
        result = {"response": response, "interrupted": interrupted}
    elif action == "reject":
        response = reject_factory_brain(
            thread_id,
            reason=payload.get("reason", "Rejected by user"),
            purpose=purpose,
        )
        result = {"response": response, "interrupted": False}
    else:
        raise ValueError(f"Unknown action: {action}")

    output_file.write_text(json.dumps(result), encoding="utf-8")
    """
)


def build_factory_agent_spec(root: Path | None = None) -> AgentSpec | None:
    project_root = (root or AGENT_FACTORY_ROOT).expanduser()
    if not project_root.exists():
        return None
    return AgentSpec(
        id="agent-factory",
        name="Agent Factory",
        purpose="Specialist agent for creating, configuring, validating, and staging agents.",
        aliases=["factory", "create-agent", "agent-builder"],
        tools=["factory_brain"],
        runtime={
            "mode": "factory_brain",
            "working_directory": str(project_root),
            "manifest_command": "uv run agent-factory manifest",
            "default_execution_mode": "instruction_only",
        },
    )


def new_factory_thread_id() -> str:
    return f"hub-factory-{uuid.uuid4()}"


def invoke_factory_request(
    *,
    working_directory: str,
    request: str,
    thread_id: str,
    purpose: str = "coding",
) -> dict[str, Any]:
    return _run_bridge(
        working_directory=working_directory,
        payload={
            "action": "invoke",
            "request": request,
            "thread_id": thread_id,
            "purpose": purpose,
        },
    )


def resume_factory_request(
    *,
    working_directory: str,
    thread_id: str,
    purpose: str = "coding",
) -> dict[str, Any]:
    return _run_bridge(
        working_directory=working_directory,
        payload={
            "action": "resume",
            "thread_id": thread_id,
            "purpose": purpose,
        },
    )


def reject_factory_request(
    *,
    working_directory: str,
    thread_id: str,
    reason: str,
    purpose: str = "coding",
) -> dict[str, Any]:
    return _run_bridge(
        working_directory=working_directory,
        payload={
            "action": "reject",
            "thread_id": thread_id,
            "purpose": purpose,
            "reason": reason,
        },
    )


def _run_bridge(*, working_directory: str, payload: dict[str, Any]) -> dict[str, Any]:
    human_logger.info("Running module: agent-factory (%s)", payload["action"])
    logger.info(
        "Running module: agent-factory (%s, thread=%s)",
        payload["action"],
        payload["thread_id"],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "factory-input.json"
        output_file = Path(tmpdir) / "factory-output.json"
        input_file.write_text(json.dumps(payload), encoding="utf-8")

        task_run_id = get_current_task_run_id()
        handle = get_task_control_registry().get_handle(task_run_id)
        if handle is not None and handle.cancel_requested:
            raise TaskCancelled(handle.cancellation_reason or "Stopped by user")

        proc = subprocess.Popen(
            [
                "uv",
                "run",
                "python",
                "-c",
                _BRIDGE_SCRIPT,
                working_directory,
                str(input_file),
                str(output_file),
            ],
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if task_run_id is not None:
            get_task_control_registry().attach_process(task_run_id, proc, agent_id="agent-factory")
        try:
            while proc.poll() is None:
                pass
            _, stderr = proc.communicate()
        finally:
            if task_run_id is not None:
                get_task_control_registry().clear_process(task_run_id)

        if handle is not None and handle.cancel_requested:
            raise TaskCancelled(handle.cancellation_reason or "Stopped by user")

        if proc.returncode != 0:
            raise RuntimeError(
                f"Agent Factory bridge failed (exit={proc.returncode}): {stderr.strip()}"
            )
        if not output_file.exists():
            raise RuntimeError("Agent Factory bridge produced no output.")
        result = json.loads(output_file.read_text(encoding="utf-8"))
        human_logger.info(
            "Module agent-factory finished (interrupted=%s)", result.get("interrupted")
        )
        return result
