"""Subprocess bridge for calling Agent Factory as a specialist agent."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import tempfile
import textwrap
import threading
import uuid
from pathlib import Path
from typing import Any

from .config import AGENT_FACTORY_ROOT
from .log_config import get_human_logger
from .progress_events import (
    PROGRESS_POLL_INTERVAL_SECONDS,
    ProgressUpdate,
    SpecialistProgressTailer,
)
from .registry import AgentSpec
from .task_control import TaskCancelled, get_task_control_registry, subprocess_popen_kwargs
from .task_runs import get_current_progress_callback, get_current_task_run_id

logger = logging.getLogger(__name__)
human_logger = get_human_logger()

_BRIDGE_SCRIPT = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path
    from pathlib import PurePosixPath

    root = Path(sys.argv[1])
    input_file = Path(sys.argv[2])
    output_file = Path(sys.argv[3])

    sys.path.insert(0, str(root / "src"))

    from agent_factory.factory_brain import (
        build_factory_specialist_result,
        invoke_factory_brain,
        reject_factory_brain,
        resume_factory_brain,
    )
    from agent_factory.agent_spec import VALID_ID_PATTERN
    from agent_factory.build_task import (
        AgentBuildTask,
        BuildTaskError,
        assert_task_matches_staged_package,
        build_task_reference,
        load_staged_manifest,
    )
    from agent_factory.progress_events import progress_reporter_from_ids
    from agent_factory.storage import get_build_task
    from agent_factory import consume_agent_build_result

    payload = json.loads(input_file.read_text(encoding="utf-8"))
    action = payload["action"]
    thread_id = payload["thread_id"]
    purpose = payload.get("purpose", "coding")
    progress_reporter = progress_reporter_from_ids(
        run_id=payload.get("run_id"),
        request_id=payload.get("request_id"),
        agent_name="Agent Factory",
    )

    def _canonical_build_task_references(values):
        candidates = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                continue
            reference = value.strip()
            path = PurePosixPath(reference)
            if (
                str(path) == reference
                and path.parts[:2] == ("staging", "agents")
                and len(path.parts) == 4
                and path.parts[3] == "BUILD_TASK.json"
                and VALID_ID_PATTERN.fullmatch(path.parts[2])
            ):
                candidates.append(reference)
        return candidates

    def _resolve_build_task(references, thread_id):
        candidates = _canonical_build_task_references(references)
        if len(candidates) != 1:
            raise BuildTaskError(
                "exactly one canonical staged BUILD_TASK.json reference is required"
            )
        reference = candidates[0]
        artifact_path = root / PurePosixPath(reference)
        try:
            task = AgentBuildTask.model_validate(
                json.loads(artifact_path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise BuildTaskError(f"invalid build-task artifact: {reference}") from exc
        if task.thread_id != thread_id or build_task_reference(task) != reference:
            raise BuildTaskError("build-task thread or artifact reference does not match")
        row = get_build_task(
            reference,
            correlation_id=task.correlation_id,
            thread_id=thread_id,
        )
        if not row or row.get("status") != "approved":
            raise BuildTaskError("build task is missing or is not approved for this thread")
        if any(
            row.get(field) != getattr(task, field)
            for field in ("agent_id", "agent_version", "manifest_sha256")
        ):
            raise BuildTaskError("approved build-task storage does not match its artifact")
        assert_task_matches_staged_package(task, root)
        package_dir = (root / task.staging_target).resolve()
        raw_manifest, manifest, _ = load_staged_manifest(package_dir, task.agent_id)
        factory_result = build_factory_specialist_result(
            thread_id,
            "Validated approved Factory build task.",
            interrupted=False,
        )
        if (
            factory_result.status != "success"
            or factory_result.artifact_reference != reference
            or factory_result.next_task is None
        ):
            raise BuildTaskError("Factory structured result does not expose this approved task")
        return {
            "status": "resolved",
            "artifact_reference": reference,
            "thread_id": thread_id,
            "correlation_id": task.correlation_id,
            "build_task": task.model_dump(mode="json"),
            "manifest": {
                "id": manifest.id,
                "version": raw_manifest["version"],
                "manifest_schema_version": raw_manifest["manifest_schema_version"],
                "name": manifest.name,
                "purpose": manifest.purpose,
                "permissions": manifest.permissions,
                "runtime": manifest.runtime,
                "design": manifest.design,
            },
            "validated_references": list(
                dict.fromkeys(
                    [reference, *task.relevant_docs, *task.relevant_skills]
                )
            ),
            "factory_result": factory_result.model_dump(mode="json"),
        }

    if action == "resolve_build_task":
        result = _resolve_build_task(payload.get("references", []), thread_id)
        output_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(0)

    if action == "consume_build_result":
        result = consume_agent_build_result(
            thread_id,
            payload["artifact_reference"],
            payload["build_result"],
            project_root=root,
        )
        output_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(0)

    if action == "invoke":
        response, interrupted = invoke_factory_brain(
            payload["request"],
            thread_id=thread_id,
            purpose=purpose,
            progress_reporter=progress_reporter,
        )
        structured = build_factory_specialist_result(
            thread_id,
            response,
            interrupted=interrupted,
        ).model_dump(mode="json")
        result = {
            "response": response,
            "interrupted": interrupted,
            **structured,
        }
    elif action == "resume":
        response, interrupted = resume_factory_brain(
            thread_id,
            purpose=purpose,
            progress_reporter=progress_reporter,
        )
        structured = build_factory_specialist_result(
            thread_id,
            response,
            interrupted=interrupted,
        ).model_dump(mode="json")
        result = {
            "response": response,
            "interrupted": interrupted,
            **structured,
        }
    elif action == "reject":
        response = reject_factory_brain(
            thread_id,
            reason=payload.get("reason", "Rejected by user"),
            purpose=purpose,
            progress_reporter=progress_reporter,
        )
        structured = build_factory_specialist_result(
            thread_id,
            response,
            interrupted=False,
        ).model_dump(mode="json")
        result = {
            "response": response,
            "interrupted": False,
            **structured,
        }
    else:
        raise ValueError(f"Unknown action: {action}")

    output_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    """
)


def _notify_progress(callback: Any | None, update: ProgressUpdate) -> None:
    if callback is None:
        return
    try:
        callback(update)
    except Exception:
        logger.exception("Factory progress notifier failed for run %s", update.run_id)


def build_factory_agent_spec(root: Path | None = None) -> AgentSpec | None:
    project_root = (root or AGENT_FACTORY_ROOT).expanduser()
    if not project_root.exists():
        return None
    return AgentSpec(
        id="agent-factory",
        name="Agent Factory",
        purpose=(
            "Primary responsibility: Design and govern new specialist agent packages.\n"
            "Select for: Creating, configuring, validating, staging, approving, rejecting, or "
            "promoting a specialist agent package as the requested deliverable.\n"
            "Do not select for: Implementing backlog items, fixing bugs, changing documentation, "
            "or modifying source code in the existing Agent Factory repository or any other "
            "existing software project."
        ),
        tools=["factory_brain"],
        task_contract={
            "task_kinds": ["agent_package_lifecycle"],
            "task_kind_descriptions": {
                "agent_package_lifecycle": (
                    "Design, create, configure, validate, stage, approve, reject, or "
                    "promote a specialist agent package as the requested deliverable."
                )
            },
            "default_task_kind": "agent_package_lifecycle",
        },
        extensions={"knowledge_db": str(project_root / "data" / "knowledge_store.sqlite3")},
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
    governed_skills: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "invoke",
        "request": request,
        "thread_id": thread_id,
        "purpose": purpose,
    }
    if governed_skills:
        payload["governed_skills"] = list(governed_skills)
    return _run_bridge(working_directory=working_directory, payload=payload)


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


def resolve_factory_build_task(
    *,
    working_directory: str,
    thread_id: str,
    references: list[str],
) -> dict[str, Any]:
    """Resolve one approved BUILD_TASK through the Factory runtime boundary."""
    result = _run_bridge(
        working_directory=working_directory,
        payload={
            "action": "resolve_build_task",
            "thread_id": thread_id,
            "references": list(references),
        },
    )
    if result.get("status") != "resolved":
        raise RuntimeError("Agent Factory returned an invalid build-task resolution")
    return result


def relay_factory_build_result(
    *,
    working_directory: str,
    thread_id: str,
    artifact_reference: str,
    build_result: dict[str, Any],
) -> dict[str, Any]:
    """Submit one terminal ATL BuildResult to Factory for authoritative validation."""
    try:
        json.dumps(build_result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ATL build_result is not JSON-safe") from exc
    result = _run_bridge(
        working_directory=working_directory,
        payload={
            "action": "consume_build_result",
            "thread_id": thread_id,
            "artifact_reference": artifact_reference,
            "build_result": build_result,
        },
    )
    if result.get("status") != "validated":
        raise RuntimeError("Agent Factory returned no validated build-result receipt")
    try:
        encoded = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Agent Factory validation receipt is not JSON-safe") from exc
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise RuntimeError("Agent Factory validation receipt exceeds the bounded limit")
    return result


def _run_bridge(*, working_directory: str, payload: dict[str, Any]) -> dict[str, Any]:
    logger.debug("Running module: agent-factory (%s)", payload["action"])
    logger.info(
        "Running module: agent-factory (%s, thread=%s)",
        payload["action"],
        payload["thread_id"],
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "factory-input.json"
        output_file = Path(tmpdir) / "factory-output.json"

        task_run_id = get_current_task_run_id()
        progress_callback = get_current_progress_callback()
        request_id = str(uuid.uuid4())
        run_payload = dict(payload)
        progress_tailer: SpecialistProgressTailer | None = None
        if task_run_id is not None:
            run_payload.update(
                {
                    "run_id": task_run_id,
                    "request_id": request_id,
                }
            )
            progress_tailer = SpecialistProgressTailer(
                run_id=task_run_id,
                request_id=request_id,
                path=Path(tmpdir) / "unused-progress-file.jsonl",
                specialist_name="Agent Factory",
            )
            for update in progress_tailer.begin():
                _notify_progress(progress_callback, update)

        input_file.write_text(json.dumps(run_payload), encoding="utf-8")

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
            **subprocess_popen_kwargs(),
        )
        if task_run_id is not None:
            get_task_control_registry().attach_process(
                task_run_id,
                proc,
                agent_id="agent-factory",
            )

        stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        stderr_lines: list[str] = []
        assert proc.stdout is not None
        assert proc.stderr is not None

        def _drain_stream(name: str, stream: Any) -> None:
            try:
                for line in iter(stream.readline, ""):
                    stream_queue.put((name, line))
            finally:
                stream_queue.put((name, None))

        reader_threads = [
            threading.Thread(
                target=_drain_stream,
                args=("stdout", proc.stdout),
                name="factory-bridge-stdout",
                daemon=True,
            ),
            threading.Thread(
                target=_drain_stream,
                args=("stderr", proc.stderr),
                name="factory-bridge-stderr",
                daemon=True,
            ),
        ]
        for thread in reader_threads:
            thread.start()

        open_streams = len(reader_threads)
        try:
            while open_streams:
                try:
                    source, line = stream_queue.get(timeout=PROGRESS_POLL_INTERVAL_SECONDS)
                except queue.Empty:
                    source = ""
                    line = ""

                if line is None:
                    open_streams -= 1
                elif source == "stderr":
                    stderr_lines.append(line.rstrip("\r\n"))
                elif source == "stdout" and progress_tailer is not None:
                    update = progress_tailer.process_line(line)
                    if update is not None:
                        _notify_progress(progress_callback, update)

                if progress_tailer is not None:
                    background = progress_tailer.maybe_emit_background_update()
                    if background is not None:
                        _notify_progress(progress_callback, background)

            proc.wait()
            for thread in reader_threads:
                thread.join(timeout=5)
            if handle is not None and handle.cancel_requested:
                raise TaskCancelled(handle.cancellation_reason or "Stopped by user")
            if progress_tailer is not None:
                progress_tailer.finish()
        finally:
            if task_run_id is not None:
                get_task_control_registry().clear_process(task_run_id)

        stderr = "\n".join(stderr_lines)

        if handle is not None and handle.cancel_requested:
            raise TaskCancelled(handle.cancellation_reason or "Stopped by user")

        if proc.returncode != 0:
            raise RuntimeError(
                f"Agent Factory bridge failed (exit={proc.returncode}): {stderr.strip()}"
            )
        if not output_file.exists():
            raise RuntimeError("Agent Factory bridge produced no output.")
        result = json.loads(output_file.read_text(encoding="utf-8"))
        if progress_tailer is not None and not result.get("interrupted"):
            progress_tailer.mark_unavailable_if_silent()
        human_logger.info(
            "Module agent-factory finished (interrupted=%s)",
            result.get("interrupted"),
        )
        return result
