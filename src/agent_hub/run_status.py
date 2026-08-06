"""Human-readable task-run status summaries."""

from __future__ import annotations

from datetime import datetime, timezone

from .progress_events import PROGRESS_STALE_AFTER_SECONDS
from .task_control import get_task_control_registry
from .task_runs import TaskRun

_OPEN_STATES = {
    "received",
    "routed",
    "dispatched",
    "in_progress",
    "waiting_clarification",
    "waiting_approval",
}


def format_current_run_status(run: TaskRun | None) -> str:
    if run is None:
        return "No task is currently active or paused."
    return _format_run(run, heading="Current task")


def format_last_run_status(run: TaskRun | None) -> str:
    if run is None:
        return "No completed or failed task has been recorded yet."
    return _format_run(run, heading="Last task")


def _format_run(run: TaskRun, *, heading: str) -> str:
    lines = [
        f"{heading}:",
        f"Run ID: {run.id}",
        f"State: {run.state}",
        f"Selected agent: {run.selected_agent_id or 'Not selected yet'}",
        f"Task summary: {_task_summary(run)}",
        f"Start time: {_format_timestamp(run.created_at)}",
        f"Duration: {_format_duration(run)}",
        f"Current phase: {_format_phase(run)}",
        f"Latest progress: {_format_progress_summary(run)}",
        f"Last specialist activity: {_format_last_activity(run)}",
        f"Live progress: {_format_live_progress(run)}",
        f"Result or error: {_result_or_error(run)}",
        f"Token usage: {_format_usage(run.usage)}",
        f"Estimated cost: {_format_cost(run.cost)}",
    ]
    return "\n".join(lines)


def _task_summary(run: TaskRun) -> str:
    summary = run.dispatched_task or run.user_message
    return _one_line(summary)


def _result_or_error(run: TaskRun) -> str:
    if run.error_message:
        return _one_line(run.error_message)
    if run.final_response:
        return _one_line(run.final_response)
    if isinstance(run.raw_result, dict):
        summary = run.raw_result.get("summary")
        if isinstance(summary, str) and summary.strip():
            return _one_line(summary)
    return "No result recorded yet"


def _format_phase(run: TaskRun) -> str:
    return run.latest_progress_phase or "Not reported yet"


def _format_progress_summary(run: TaskRun) -> str:
    return run.latest_progress_summary or "No progress reported yet"


def _format_last_activity(run: TaskRun) -> str:
    latest = _latest_activity(run)
    if latest is None:
        return "Not recorded"
    age = _relative_age(latest)
    return f"{_format_timestamp(latest)} ({age} ago)"


def _format_live_progress(run: TaskRun) -> str:
    if run.progress_mode == "pending":
        return "Waiting for first streamed update"
    if run.progress_mode == "unavailable":
        return "Unavailable from specialist"
    if run.progress_mode == "streaming":
        latest = _latest_activity(run)
        handle = get_task_control_registry().get_handle(run.id)
        process_alive = bool(
            handle is not None and handle.process is not None and handle.process.poll() is None
        )
        if process_alive and latest is not None:
            silence = (datetime.now(timezone.utc) - latest).total_seconds()
            if silence > PROGRESS_STALE_AFTER_SECONDS:
                return f"Stale ({_format_duration_ms(int(silence * 1000))} since last update)"
        return "Active"
    return "Not tracked"


def _format_usage(usage: dict | None) -> str:
    if not isinstance(usage, dict):
        return "Not recorded"

    totals = usage.get("totals")
    if not isinstance(totals, dict):
        return "Not recorded"

    total_tokens = totals.get("total_tokens")
    input_tokens = totals.get("input_tokens")
    output_tokens = totals.get("output_tokens")
    if not all(isinstance(value, int) for value in (total_tokens, input_tokens, output_tokens)):
        return "Not recorded"

    return (
        f"total={total_tokens}, input={input_tokens}, output={output_tokens}"
    )


def _format_cost(cost: dict | None) -> str:
    if not isinstance(cost, dict):
        return "Not recorded"

    status = cost.get("status")
    known_usd = cost.get("known_usd")
    unknown_models = cost.get("unknown_models")

    if status == "estimated" and isinstance(known_usd, (int, float)):
        return f"${known_usd:.6f} (estimated)"
    if status == "partial" and isinstance(known_usd, (int, float)):
        unknown = ", ".join(str(item) for item in unknown_models or []) or "unknown model"
        return f"${known_usd:.6f} known (partial; unresolved: {unknown})"
    if status == "unknown":
        return "Unknown"
    return "Not recorded"


def _format_duration(run: TaskRun) -> str:
    if isinstance(run.duration_ms, int):
        return _format_duration_ms(run.duration_ms)
    if run.finished_at is not None:
        elapsed = max(0, int((run.finished_at - run.created_at).total_seconds() * 1000))
        return _format_duration_ms(elapsed)
    if run.state in _OPEN_STATES:
        now = datetime.now(timezone.utc)
        elapsed = max(0, int((now - run.created_at).total_seconds() * 1000))
        return f"{_format_duration_ms(elapsed)} so far"
    return "Not available"


def _format_duration_ms(duration_ms: int) -> str:
    total_seconds = duration_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    milliseconds = duration_ms % 1000
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    if total_seconds:
        return f"{total_seconds}s"
    return f"{milliseconds}ms"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _latest_activity(run: TaskRun) -> datetime | None:
    candidates = [ts for ts in (run.last_progress_event_at, run.last_progress_heartbeat_at) if ts]
    if not candidates:
        return None
    return max(candidates)


def _relative_age(value: datetime) -> str:
    delta_ms = max(
        0,
        int((datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds() * 1000),
    )
    return _format_duration_ms(delta_ms)


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if collapsed else "Not recorded"
