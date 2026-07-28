"""LangGraph orchestrator — routes tasks to specialist agents via subprocess dispatch."""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .checkpointer import get_checkpointer
from .config import DEFAULT_MODEL
from .cost_log import extract_usage_metadata, record_llm_run
from .factory_bridge import (
    build_factory_agent_spec,
    invoke_factory_request,
    new_factory_thread_id,
    reject_factory_request,
    resume_factory_request,
)
from .hub_context import HubContextService
from .hub_memory import (
    ExtractionCandidate,
    HubMemoryManager,
    analyze_learning,
    extract_semantic_candidates,
    format_forget_confirmation,
    format_learning_confirmation,
    format_learning_list,
    format_learnings_for_prompt,
)
from .hub_skills import HubSkillStore, SkillProposalResult
from .knowledge_store import get_knowledge_store
from .learning_mode import get_learning_mode_registry
from .log_config import get_human_logger
from .manifest_cache import get_manifest_cache
from .progress_events import (
    PROGRESS_POLL_INTERVAL_SECONDS,
    ProgressUpdate,
    SpecialistProgressTailer,
)
from .project_context import (
    PROJECT_CONTRACT_VERSION,
    ProjectContext,
    ProjectContextResolution,
    get_project_context_registry,
)
from .registry import (
    AgentSpec,
    RegistryLoadError,
    load_registry_report,
    spec_fingerprint,
)
from .run_status import format_current_run_status, format_last_run_status
from .session_state import load_or_create_session_id, persist_session_id
from .shared_docs import make_shared_docs_tool
from .task_control import TaskCancelled, get_task_control_registry, subprocess_popen_kwargs
from .task_envelope import build_task_envelope
from .task_runs import (
    DEFAULT_PROJECT_KEY,
    TASK_STATE_CANCELLED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    TASK_STATE_WAITING_DECISION,
    TaskRun,
    active_task_run,
    get_current_progress_callback,
    get_current_task_run_id,
    get_task_run_store,
    is_active_state,
    is_paused_state,
    is_terminal_state,
)

logger = logging.getLogger(__name__)
human_logger = get_human_logger()


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _project_key_for_session(session_id: str) -> str:
    context = get_project_context_registry().get(session_id)
    return context.project_id if context is not None else DEFAULT_PROJECT_KEY


def _friendly_project_label(project_key: str) -> str:
    return "default" if project_key == DEFAULT_PROJECT_KEY else project_key


def _human_task_log(task_run_id: str | None, message: str, *args: Any) -> None:
    if task_run_id:
        human_logger.info("Task %s: " + message, task_run_id[:8], *args)
        return
    human_logger.info(message, *args)


def _resolve_project_context_for_task_run(task_run_id: str | None) -> ProjectContextResolution:
    """Resolve and revalidate the operator's /project selection for a fresh dispatch.

    project_root is passed through as a request, not a grant — the
    specialist enforces its own allowlist server-side and rejects it with a
    clear failed status if the path isn't permitted. This only covers a
    *fresh* dispatch: a resumed dispatch replays the exact context pinned at
    the original dispatch instead of re-resolving against whatever
    /project now points at (see `project_context_override` in
    `_dispatch_subprocess`).
    """
    if not task_run_id:
        return ProjectContextResolution(context=None, error=None)
    run = get_task_run_store().get_run(task_run_id)
    if run is None:
        return ProjectContextResolution(context=None, error=None)
    return get_project_context_registry().resolve_for_dispatch(run.session_id)


def _project_context_override_from_pending(pending: TaskRun) -> ProjectContext | None:
    """Rebuild the `ProjectContext` pinned at a paused task's original dispatch.

    Reconstructed from the flat fields `_dispatch_subprocess` stored in the
    task's context (`agent_dispatch_project_*`), not by re-resolving
    `/project` — a resumed dispatch must replay exactly what the original
    dispatch validated and sent, even if the operator's live selection has
    since changed or gone stale.
    """
    project_id = pending.context.get("agent_dispatch_project_id")
    if not project_id:
        return None
    return ProjectContext(
        project_id=project_id,
        root=pending.context.get("agent_dispatch_project_root") or "",
        contract_version=(
            pending.context.get("agent_dispatch_project_contract_version")
            or PROJECT_CONTRACT_VERSION
        ),
        fingerprint=pending.context.get("agent_dispatch_project_fingerprint") or "",
        metadata={},
    )


def _emit_progress_update(update: ProgressUpdate) -> None:
    callback = get_current_progress_callback()
    if callback is None:
        return
    try:
        callback(update)
    except Exception:
        logger.exception("Progress notifier failed for run %s", update.run_id)

_SYSTEM_PROMPT = """You are the Agent Hub orchestrator. You coordinate specialist AI agents.

Select the specialist using only each tool's purpose. Treat the purpose as the
complete routing contract: primary responsibility; select for; do not select for.
Match the user's requested action to that contract.

Use the purpose as the only routing contract. Do not infer specialist scope
from an agent name, project name, or alias.

A named project is the target of the work, not automatically the specialist.
A request naming a project is not routed to that project's own agent unless
that agent's purpose is the one being asked for.

When a user sends a request:
1. Select the correct agent from your tool list based only on their purpose.
2. Call that agent's tool with a clear, bounded task description.
3. Return the agent's result to the user.

If the user gives an explicit pointer — a file path, URL, or ID — pass it via
the tool's `references` argument verbatim instead of paraphrasing it into the
task description. Do not interpret what a reference means; only relay it.

Do not explain what you would do — invoke the agent and return the result.
Do not answer coding, research, or creation tasks yourself — that is the specialist agent's job.

If the agent returns a clarification question, relay it to the user verbatim.
If the agent requires approval, tell the user exactly what needs approval and wait.
If no purpose clearly matches the request, ask the user for clarification instead
of guessing."""


def _build_system_prompt(state: Any) -> list[Any]:
    """Assemble the system prompt fresh per turn, folding in operator-stored learnings.

    Reading the knowledge store on every model call (rather than baking learnings
    into the prompt at graph-construction time) means a /learn or /forget takes
    effect on the very next turn without restarting the hub.
    """
    messages = (
        state.get("messages", [])
        if isinstance(state, dict)
        else getattr(state, "messages", [])
    )
    learnings_block = format_learnings_for_prompt(HubMemoryManager().list_learnings())
    content = f"{_SYSTEM_PROMPT}\n\n{learnings_block}" if learnings_block else _SYSTEM_PROMPT
    return [SystemMessage(content=content)] + list(messages)


def _message_preview(message: Any) -> str | None:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        names = [
            call.get("name", "unknown-tool")
            for call in tool_calls
            if isinstance(call, dict)
        ]
        return f"requested tool call(s): {', '.join(names)}"

    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        return f"produced message: {_truncate(content.strip())}"
    if content:
        return f"produced message: {_truncate(str(content))}"

    name = getattr(message, "name", None)
    if name:
        return f"updated message state for {name}"
    return None


def _update_preview(payload: Any) -> str:
    if isinstance(payload, dict):
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            preview = _message_preview(messages[-1])
            if preview:
                return preview

        keys = [key for key in sorted(payload) if key != "messages"]
        if keys:
            return f"updated state key(s): {', '.join(keys)}"
        return "updated state"

    if payload is None:
        return "ran"

    return f"updated state: {_truncate(str(payload))}"


def _graph_node_label(node_name: str) -> str:
    return f"[{node_name}]"


def _graph_path_label(path: list[str]) -> str:
    return " -> ".join(_graph_node_label(node_name) for node_name in path)


def _append_graph_step(
    graph_steps: list[tuple[str, str]],
    node_name: str,
    summary: str,
) -> None:
    if graph_steps and graph_steps[-1] == (node_name, summary):
        return
    graph_steps.append((node_name, summary))


def _render_graph_trace(
    graph_path: list[str],
    graph_steps: list[tuple[str, str]],
) -> str | None:
    if not graph_path:
        return None

    lines = [
        "LangGraph node path:",
        f"  {_graph_path_label(graph_path)}",
    ]
    if graph_steps:
        for index, (node_name, summary) in enumerate(graph_steps):
            branch = "`--" if index == len(graph_steps) - 1 else "|--"
            lines.append(f"  {branch} {_graph_node_label(node_name)} {summary}")
    return "\n".join(lines)


def _consume_graph_stream_event(
    task_run_id: str,
    event: Any,
    last_node: str | None,
    graph_path: list[str],
    graph_steps: list[tuple[str, str]],
) -> tuple[str | None, dict[str, Any] | None]:
    namespace: tuple[Any, ...] = ()
    mode: str | None = None
    data: Any = None

    if isinstance(event, tuple):
        if len(event) == 3:
            namespace, mode, data = event
        elif len(event) == 2:
            mode, data = event
    if mode is None:
        return last_node, data if isinstance(data, dict) else None

    namespace_prefix = ""
    if namespace:
        namespace_prefix = f" within {'/'.join(str(part) for part in namespace)}"

    if mode == "tasks" and isinstance(data, dict):
        name = data.get("name") or data.get("node") or data.get("task")
        if name:
            if data.get("error"):
                # A real failure inside the graph is worth surfacing to the
                # human log even though the rest of this function is debug-only
                # tracing — it's a state change (something broke), not noise.
                _human_task_log(
                    task_run_id,
                    "LangGraph task '%s'%s failed: %s",
                    name,
                    namespace_prefix,
                    _truncate(str(data['error'])),
                )
            elif data.get("interrupts"):
                logger.debug(
                    "Task %s: LangGraph task '%s'%s interrupted.",
                    task_run_id,
                    name,
                    namespace_prefix,
                )
            # "started"/"finished" are intentionally not logged here — the
            # compact graph path plus node summary lines already cover the
            # non-error, non-interrupt flow.
        return last_node, None

    if mode == "updates" and isinstance(data, dict):
        for node_name, payload in data.items():
            if last_node is None:
                logger.debug("Task %s: LangGraph entered node '%s'.", task_run_id, node_name)
            elif last_node != node_name:
                logger.debug(
                    "Task %s: LangGraph rerouted from '%s' to '%s'.",
                    task_run_id,
                    last_node,
                    node_name,
                )
            if not graph_path or graph_path[-1] != node_name:
                graph_path.append(node_name)
            logger.debug(
                "Task %s: Node '%s'%s %s.",
                task_run_id,
                node_name,
                namespace_prefix,
                _update_preview(payload),
            )
            _append_graph_step(graph_steps, node_name, _update_preview(payload))
            last_node = node_name
        return last_node, None

    if mode == "values" and isinstance(data, dict):
        return last_node, data

    return last_node, None


def _accepted_context_keys(spec: AgentSpec) -> set[str]:
    """`input_contract.accepted_context` this specialist declares (see
    `_resolve_dispatch_context`); a specialist with no declaration is
    treated as accepting both universal context keys."""
    input_contract = spec.input_contract
    if "accepted_context" in input_contract:
        return set(input_contract.get("accepted_context") or [])
    return {"project_root", "references"}


def _resolve_dispatch_context(
    spec: AgentSpec,
    *,
    project_root: str | None,
    references: list[str] | None,
) -> tuple[str | None, list[str] | None, list[str]]:
    """Narrow project_root/references to what this specialist's manifest declares.

    `input_contract.accepted_context` declares which of `project_root`/
    `references` a specialist reads at all; a specialist that never declares
    it is treated as accepting both, preserving the universal default every
    specialist written before this declaration existed already gets.
    `input_contract.required_context` (a subset of `accepted_context`) names
    context the specialist cannot function without. Returns the narrowed
    project_root/references plus the names of any required context this
    dispatch doesn't actually have — an empty list means the dispatch is valid.
    """
    accepted = _accepted_context_keys(spec)
    required = set(spec.input_contract.get("required_context") or [])

    resolved_project_root = project_root if "project_root" in accepted else None
    resolved_references = references if (references and "references" in accepted) else None

    missing = [
        name
        for name, value in (
            ("project_root", resolved_project_root),
            ("references", resolved_references),
        )
        if name in required and not value
    ]
    return resolved_project_root, resolved_references, missing


HUB_SUPPORTED_PROJECT_CONTEXT_SCHEMA_VERSIONS = {1}
"""schema_version values Hub itself knows how to build a `project_context`
envelope for. Not a specialist's declaration — Hub's own side of the
negotiation in `_resolve_project_context_schema_version`."""


class ProjectContextVersionError(ValueError):
    """A specialist declared `project_context_contract` but Hub shares no
    schema_version with it — dispatch must stop rather than guess one."""


def _resolve_project_context_schema_version(spec: AgentSpec) -> int | None:
    """Negotiate the project_context.schema_version to send this specialist.

    Reads the specialist's own declared `project_context_contract
    .supported_schema_versions` — already loaded onto `spec` by the registry —
    and picks the highest version both Hub and the specialist support. Hub
    never hardcodes or guesses a version for a specialist it hasn't verified
    support from.

    Returns `None` when the specialist hasn't declared this contract at all:
    a legacy specialist Hub still dispatches, using only the flat
    `project_root`/`references` fields exactly as before. Raises
    `ProjectContextVersionError` when the specialist *has* declared the
    contract but Hub shares no compatible version with it — that dispatch
    must be refused, not sent with a guessed version.
    """
    contract = spec.project_context_contract
    if not isinstance(contract, dict) or not contract:
        contract = spec.input_contract.get("project_context_contract")
    if not isinstance(contract, dict) or not contract:
        return None

    supported = contract.get("supported_schema_versions")
    if not isinstance(supported, list) or not supported:
        return None

    compatible = sorted(
        version
        for version in supported
        if isinstance(version, int)
        and version in HUB_SUPPORTED_PROJECT_CONTEXT_SCHEMA_VERSIONS
    )
    if not compatible:
        raise ProjectContextVersionError(
            f"{spec.name} declares project_context_contract.supported_schema_versions="
            f"{supported!r}, but Hub only supports "
            f"{sorted(HUB_SUPPORTED_PROJECT_CONTEXT_SCHEMA_VERSIONS)!r}. Refusing to "
            "dispatch with a version this specialist hasn't confirmed it understands."
        )
    return compatible[-1]


def _dispatch_subprocess(
    spec: AgentSpec,
    task: str,
    *,
    references: list[str] | None = None,
    human_approved: bool = False,
    approval_token: str | None = None,
    request_id: str | None = None,
    resume: Any | None = None,
    decision: dict[str, Any] | None = None,
    project_root_override: str | None = None,
    project_context_override: ProjectContext | None = None,
) -> dict:
    """Invoke a subprocess specialist and return its structured JSON output.

    `resume` is an opaque value a specialist itself issued (its own
    `resume_token`) when it last paused for clarification — Hub relays it
    unchanged and never inspects its contents. `decision` answers a
    specialist's generic `pending_decision` pause (see `provide_decision`)
    and is identified by resubmitting the same `request_id`, not `resume`.
    `project_root_override`/`project_context_override` replay the exact
    project the *original* dispatch used, for a resumed call, instead of
    re-resolving the operator's current `/project` selection (which could
    have changed, or gone stale, while the task was paused).
    """
    runtime = spec.runtime
    entrypoint = runtime["entrypoint"]
    working_dir = runtime["working_directory"]

    request_id = request_id or str(uuid.uuid4())
    task_run_id = get_current_task_run_id()
    if project_root_override is not None:
        project_root = project_root_override
        project_context = project_context_override
        project_context_error = None
    else:
        resolution = _resolve_project_context_for_task_run(task_run_id)
        project_context = resolution.context
        project_context_error = resolution.error
        project_root = project_context.root if project_context is not None else None
    project_root, references, missing_context = _resolve_dispatch_context(
        spec, project_root=project_root, references=references
    )
    if project_context_error and "project_root" in _accepted_context_keys(spec):
        output = {"status": "failed", "summary": project_context_error}
        _record_agent_status(spec, output, task_run_id)
        return output
    if missing_context:
        detail = (
            f"{spec.name} requires {' and '.join(missing_context)} to run, but none "
            "was available for this task."
        )
        if "project_root" in missing_context:
            detail += " Set one with /project <path> and try again."
        output = {"status": "failed", "summary": detail}
        _record_agent_status(spec, output, task_run_id)
        return output

    try:
        project_context_schema_version = _resolve_project_context_schema_version(spec)
    except ProjectContextVersionError as exc:
        output = {"status": "failed", "summary": str(exc)}
        _record_agent_status(spec, output, task_run_id)
        return output

    envelope_project_context = (
        {
            "schema_version": project_context_schema_version,
            "project_root": project_root,
            "references": references or [],
        }
        if project_context_schema_version is not None
        else None
    )

    envelope_project_id = project_context.project_id if (project_root and project_context) else None
    envelope_project_contract_version = (
        project_context.contract_version if (project_root and project_context) else None
    )
    envelope_project_fingerprint = (
        project_context.fingerprint if (project_root and project_context) else None
    )

    if task_run_id:
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_DISPATCHED,
            detail=f"Dispatched task to specialist agent '{spec.id}'.",
            selected_agent_id=spec.id,
            dispatched_task=task,
            context_updates={
                "agent_request_id": request_id,
                "runtime_mode": "subprocess",
                "agent_dispatch_project_root": project_root,
                "agent_dispatch_references": references,
                "agent_dispatch_project_id": envelope_project_id,
                "agent_dispatch_project_contract_version": envelope_project_contract_version,
                "agent_dispatch_project_fingerprint": envelope_project_fingerprint,
                "pinned_agent_spec": dataclasses.asdict(spec),
                "pinned_agent_version": spec.version,
                "pinned_agent_fingerprint": spec_fingerprint(spec),
            },
            human_log=False,
        )
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_IN_PROGRESS,
            detail=f"Specialist agent '{spec.id}' is running.",
            selected_agent_id=spec.id,
            human_log=False,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "input.json"
        output_file = Path(tmpdir) / "output.json"
        progress_file = Path(tmpdir) / "progress.jsonl"

        if project_root:
            _human_task_log(
                task_run_id,
                "Passing project path to %s: %s",
                spec.name,
                project_root,
            )
        else:
            logger.debug("Dispatching %s with no project_root (specialist default)", spec.id)
        if references:
            _human_task_log(
                task_run_id,
                "Passing %d reference(s) to %s: %s",
                len(references),
                spec.name,
                ", ".join(references),
            )
        else:
            logger.debug("Dispatching %s with no references", spec.id)
        input_data = build_task_envelope(
            task=task,
            request_id=request_id,
            run_id=task_run_id,
            source="agent-hub",
            execution_mode=runtime["default_execution_mode"],
            progress_jsonl=str(progress_file),
            project_root=project_root,
            project_id=envelope_project_id,
            project_contract_version=envelope_project_contract_version,
            project_fingerprint=envelope_project_fingerprint,
            project_context=envelope_project_context,
            references=references,
            human_approved=human_approved,
            approval_token=approval_token,
            resume=resume,
            decision=decision,
        )
        input_file.write_text(json.dumps(input_data, indent=2), encoding="utf-8")

        input_arg = runtime["input_arg"]
        output_arg = runtime["output_arg"]
        cmd = entrypoint.split() + [input_arg, str(input_file), output_arg, str(output_file)]

        _human_task_log(
            task_run_id,
            "Calling %s (%s) with: %s",
            spec.name,
            spec.id,
            _truncate(task),
        )
        logger.info("Dispatching to %s (request_id=%s): %s", spec.id, request_id, task[:120])
        handle = get_task_control_registry().get_handle(task_run_id)
        cancelled_run = get_task_run_store().get_run(task_run_id) if task_run_id else None
        if (
            (handle is not None and handle.cancel_requested)
            or (cancelled_run is not None and cancelled_run.state == TASK_STATE_CANCELLED)
        ):
            reason = "Stopped by user"
            if handle is not None and handle.cancellation_reason:
                reason = handle.cancellation_reason
            elif cancelled_run is not None and cancelled_run.cancellation_reason:
                reason = cancelled_run.cancellation_reason
            raise TaskCancelled(reason)

        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **subprocess_popen_kwargs(),
        )
        progress_tailer = SpecialistProgressTailer(
            run_id=task_run_id or request_id,
            request_id=request_id,
            path=progress_file,
            specialist_name=spec.name,
        )
        for update in progress_tailer.begin():
            _emit_progress_update(update)
        if task_run_id is not None:
            get_task_control_registry().attach_process(task_run_id, proc, agent_id=spec.id)
        try:
            while True:
                for update in progress_tailer.poll():
                    _emit_progress_update(update)
                background = progress_tailer.maybe_emit_background_update()
                if background is not None:
                    _emit_progress_update(background)
                if proc.poll() is not None:
                    break
                time.sleep(PROGRESS_POLL_INTERVAL_SECONDS)
            for update in progress_tailer.poll(final=True):
                _emit_progress_update(update)
            progress_tailer.finish()
            _, stderr = proc.communicate()
        finally:
            if task_run_id is not None:
                get_task_control_registry().clear_process(task_run_id)

        if handle is not None and handle.cancel_requested:
            raise TaskCancelled(handle.cancellation_reason or "Stopped by user")

        if not output_file.exists():
            raise RuntimeError(
                f"Agent '{spec.id}' subprocess (exit={proc.returncode}) wrote no output.\n"
                f"stderr: {stderr.strip()}"
            )

        output = json.loads(output_file.read_text(encoding="utf-8"))
        if output.get("status") not in (
            "needs_clarification",
            "approval_required",
        ) and _valid_pending_decision(output.get("pending_decision")) is None:
            progress_tailer.ensure_progress_started()
        _human_task_log(
            task_run_id,
            "%s finished with status '%s'.",
            spec.name,
            output.get("status", "unknown"),
        )
        _update_manifest_cache(spec, output)
        if task_run_id:
            get_task_run_store().update_run(task_run_id, raw_result=output)
        _record_agent_status(spec, output, task_run_id)
        return output


def _dispatch_factory_brain(
    spec: AgentSpec,
    task: str,
    *,
    thread_id: str | None = None,
    action: str = "invoke",
    reject_reason: str | None = None,
) -> dict:
    runtime = spec.runtime
    working_directory = runtime["working_directory"]
    resolved_thread_id = thread_id or new_factory_thread_id()
    task_run_id = get_current_task_run_id()
    _human_task_log(
        task_run_id,
        "Calling %s (%s) in %s mode.",
        spec.name,
        spec.id,
        action,
    )
    if task_run_id:
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_DISPATCHED,
            detail=f"Dispatched task to specialist agent '{spec.id}'.",
            selected_agent_id=spec.id,
            dispatched_task=task,
            context_updates={
                "agent_thread_id": resolved_thread_id,
                "runtime_mode": "factory_brain",
                "pinned_agent_spec": dataclasses.asdict(spec),
                "pinned_agent_version": spec.version,
                "pinned_agent_fingerprint": spec_fingerprint(spec),
            },
            human_log=False,
        )
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_IN_PROGRESS,
            detail=f"Specialist agent '{spec.id}' is running.",
            selected_agent_id=spec.id,
            human_log=False,
        )

    if action == "resume":
        result = resume_factory_request(
            working_directory=working_directory,
            thread_id=resolved_thread_id,
        )
    elif action == "reject":
        result = reject_factory_request(
            working_directory=working_directory,
            thread_id=resolved_thread_id,
            reason=reject_reason or "Rejected by user",
        )
    else:
        result = invoke_factory_request(
            working_directory=working_directory,
            request=task,
            thread_id=resolved_thread_id,
        )

    cache = get_manifest_cache().get_or_refresh(spec)
    output = {
        "status": "approval_required" if result.get("interrupted") else "success",
        "summary": result.get("response", ""),
        "agent_manifest": (
            {
                "agent_id": spec.id,
                "manifest_hash": cache.manifest_hash,
                "manifest_command": cache.manifest_command,
            }
            if cache is not None
            else None
        ),
    }
    if task_run_id:
        get_task_run_store().update_run(
            task_run_id,
            raw_result=output,
            context_updates={"agent_thread_id": resolved_thread_id},
        )
    _human_task_log(
        task_run_id,
        "%s finished with status '%s'.",
        spec.name,
        output["status"],
    )
    _record_agent_status(spec, output, task_run_id)
    return output


def _valid_pending_decision(value: Any) -> dict[str, Any] | None:
    """Return `value` if it's a well-formed generic decision descriptor, else None.

    A specialist reports `pending_decision` as `{prompt, options, ...}` when
    it pauses on something it can describe generically. Hub only ever reads
    `prompt` (text to show the user) and each option's `name` (the exact
    `decision.option` values valid right now) — any other key (e.g. a
    specialist's own internal `kind`) is stored and relayed back opaquely,
    never interpreted. A missing or empty `options` list means the
    specialist itself couldn't describe the pause generically, so Hub
    treats it as absent rather than offering the user nothing to pick from.
    """
    if not isinstance(value, dict):
        return None
    options = value.get("options")
    if not isinstance(options, list) or not options:
        return None
    for option in options:
        if not isinstance(option, dict) or not str(option.get("name", "")).strip():
            return None
    return value


def _describe_decision_option(option: dict[str, Any]) -> str:
    name = option["name"]
    return f"{name} (needs text)" if option.get("needs_text") else str(name)


def _format_pending_decision(spec: AgentSpec, pending_decision: dict[str, Any]) -> str:
    prompt = str(pending_decision.get("prompt", "")).strip() or "A decision is required."
    options_text = ", ".join(
        _describe_decision_option(option) for option in pending_decision["options"]
    )
    return (
        f"[{spec.name}] Decision needed: {prompt}\n"
        f"Reply with /decide <option> [text] — allowed options: {options_text}."
    )


def _format_output(spec: AgentSpec, output: dict) -> str:
    """Convert agent output JSON to a string for the orchestrator LLM."""
    status = output.get("status", "unknown")
    summary = str(output.get("summary", "")).strip()

    if status == "success":
        instruction = str(output.get("coding_agent_instruction", "")).strip()
        if summary and instruction:
            return f"[{spec.name}] {summary}\n\n{instruction}".strip()
        if instruction:
            return f"[{spec.name}] {instruction}".strip()
        return f"[{spec.name}] {summary}".strip()

    pending_decision = _valid_pending_decision(output.get("pending_decision"))
    if pending_decision is not None:
        return _format_pending_decision(spec, pending_decision)

    if status == "needs_clarification":
        return f"[{spec.name}] Clarification needed: {summary}"

    if status == "approval_required":
        token = output.get("approval_token", "")
        if token:
            return (
                f"[{spec.name}] Approval required: {summary}\n"
                f"Approval token: {token}\n"
                "Use /approve to continue or /reject <reason> to stop."
            )
        return (
            f"[{spec.name}] Approval required: {summary}\n"
            "Use /approve to continue or /reject <reason> to stop."
        )

    if status == "failed":
        return f"[{spec.name}] Failed: {summary or 'no summary provided.'}"

    raise RuntimeError(
        f"Agent '{spec.id}' returned status '{status}': {summary or output}"
    )


_RELAY_VERBATIM_MARKERS = (
    "] Clarification needed:",
    "] Approval required:",
    "] Decision needed:",
)


def _relay_specialist_terminal_message(messages: list[Any]) -> str | None:
    """Return the specialist's own clarification/approval text verbatim, if the
    graph's final answer immediately followed one.

    create_react_agent always routes tool results back through the LLM for a
    final reply, even when the tool result is already the exact structured
    question the user needs to see (see _format_output). Rewriting that text
    adds nothing and risks paraphrasing away detail (or the approval token),
    so for those two terminal statuses Hub relays the specialist's own text
    instead of the model's second-pass rewrite.
    """
    for message in reversed(messages[:-1]):
        if isinstance(message, HumanMessage):
            return None
        if isinstance(message, ToolMessage):
            content = message.content
            if isinstance(content, str) and content.startswith("["):
                first_line = content.split("\n", 1)[0]
                if any(marker in first_line for marker in _RELAY_VERBATIM_MARKERS):
                    return content
            return None
    return None


_MAX_RESUME_TOKEN_BYTES = 8192


def _bounded_resume_token(value: Any) -> Any | None:
    """Return `value` if it's JSON-serializable and reasonably small, else None.

    A specialist's resume_token is opaque to Hub — this only guards against
    an oversized or non-serializable value corrupting the stored task
    context, never against anything about what the token means.
    """
    if value is None:
        return None
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError):
        return None
    if len(encoded.encode("utf-8")) > _MAX_RESUME_TOKEN_BYTES:
        return None
    return value


def _record_agent_status(spec: AgentSpec, output: dict, task_run_id: str | None) -> None:
    if not task_run_id:
        return

    store = get_task_run_store()
    status = output.get("status", "unknown")
    summary = output.get("summary", "")
    pending_decision = _valid_pending_decision(output.get("pending_decision"))

    if pending_decision is not None:
        prompt = str(pending_decision.get("prompt", "")).strip()
        detail = f"{spec.name} needs a decision" + (f": {prompt}" if prompt else ".")
        store.transition(
            task_run_id,
            TASK_STATE_WAITING_DECISION,
            detail=detail,
            selected_agent_id=spec.id,
            context_updates={"specialist_pending_decision": pending_decision},
        )
    elif status == "needs_clarification":
        detail = f"{spec.name} requested clarification" + (f": {summary}" if summary else ".")
        raw_resume_token = output.get("resume_token")
        resume_token = _bounded_resume_token(raw_resume_token)
        if raw_resume_token is not None and resume_token is None:
            logger.warning(
                "Task %s: %s returned an oversized or non-serializable resume_token; "
                "treating it as absent.",
                task_run_id,
                spec.name,
            )
        store.transition(
            task_run_id,
            TASK_STATE_WAITING_CLARIFICATION,
            detail=detail,
            selected_agent_id=spec.id,
            context_updates={"specialist_resume_token": resume_token},
        )
    elif status == "approval_required":
        detail = f"{spec.name} requested approval" + (f": {summary}" if summary else ".")
        store.transition(
            task_run_id,
            TASK_STATE_WAITING_APPROVAL,
            detail=detail,
            selected_agent_id=spec.id,
            approval_token=output.get("approval_token"),
        )
    elif status == "failed":
        detail = f"{spec.name} reported a failure" + (f": {summary}" if summary else ".")
        store.transition(
            task_run_id,
            TASK_STATE_FAILED,
            detail=detail,
            selected_agent_id=spec.id,
            error_message=summary,
        )


def _update_manifest_cache(spec: AgentSpec, output: dict) -> None:
    reference = output.get("agent_manifest")
    if isinstance(reference, dict):
        get_manifest_cache().update_reference(spec.id, reference)


def _make_agent_tool(spec: AgentSpec) -> Any:
    """Create a LangChain tool that dispatches to the registered agent."""
    mode = spec.runtime["mode"]
    description = get_manifest_cache().description_for(spec)

    @lc_tool(spec.id, description=description)
    def _call_agent(task: str, references: list[str] | None = None) -> str:
        task_run_id = get_current_task_run_id()
        if task_run_id:
            get_task_run_store().transition(
                task_run_id,
                TASK_STATE_ROUTED,
                detail=f"Routed to {spec.name}.",
                selected_agent_id=spec.id,
            )
        if mode == "subprocess":
            return _format_output(
                spec, _dispatch_subprocess(spec, task, references=references)
            )
        if mode == "factory_brain":
            return _format_output(spec, _dispatch_factory_brain(spec, task))
        raise RuntimeError(f"Unsupported runtime mode: {mode}")

    return _call_agent


def _build_memory_tools(store: Any, registry: list[AgentSpec]) -> list[Any]:
    return [make_shared_docs_tool(registry)]


def _repair_dangling_tool_calls(graph: Any, thread_id: str, reason: str) -> list[str]:
    """Close out any checkpointed AIMessage tool_calls left without a ToolMessage.

    A specialist dispatch killed mid-call (process crash, forced subprocess
    termination, Ctrl-C/SIGTERM to the whole Hub process) can leave the
    LangGraph checkpoint for a thread holding an AIMessage that requested a
    tool call with no matching ToolMessage. The next graph call on that same
    thread then fails LangGraph's chat-history validation before ever
    reaching the model. Since session_id now survives a restart (see
    AGENT-HUB-032), that corruption is persistent rather than quietly
    discarded by a fresh thread, so it must be repaired here, defensively,
    before every graph call.
    """
    if not hasattr(graph, "get_state") or not hasattr(graph, "update_state"):
        return []
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = graph.get_state(config)
    except Exception:
        logger.exception("Could not read graph state for thread %s while repairing", thread_id)
        return []
    values = getattr(snapshot, "values", None) if snapshot is not None else None
    messages = list(values.get("messages", [])) if isinstance(values, dict) else []
    if not messages:
        return []

    answered_ids = {
        getattr(message, "tool_call_id", None)
        for message in messages
        if isinstance(message, ToolMessage)
    }
    repaired_ids: list[str] = []
    synthetic_messages: list[ToolMessage] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if not call_id or call_id in answered_ids:
                continue
            call_name = (
                call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            ) or "unknown-tool"
            synthetic_messages.append(
                ToolMessage(content=f"Cancelled: {reason}", tool_call_id=call_id, name=call_name)
            )
            repaired_ids.append(call_id)

    if synthetic_messages:
        graph.update_state(config, {"messages": synthetic_messages})
        human_logger.info(
            "Hub repaired %d interrupted specialist call(s) left over from a previous "
            "session before continuing.",
            len(synthetic_messages),
        )
        logger.warning(
            "Repaired %d dangling tool call(s) on thread %s: %s",
            len(synthetic_messages),
            thread_id,
            repaired_ids,
        )
    return repaired_ids


class HubOrchestrator:
    """Stateful orchestrator with per-session thread isolation."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        semantic_extractor: Any = None,
        learning_analyzer: Any = None,
        skill_store: Any = None,
        context_service: Any = None,
    ) -> None:
        self._model = model
        self._registry = _load_specialists()
        self._registry_errors: list[RegistryLoadError] = _load_registry_errors()
        self._registry_last_refreshed = _utcnow_iso()
        self._session_id = load_or_create_session_id()
        self._semantic_extractor = semantic_extractor or extract_semantic_candidates
        self._learning_analyzer = learning_analyzer or analyze_learning
        self._skill_store = skill_store or HubSkillStore()
        self._context_service = context_service or HubContextService()
        self._learning_notify: Any = None
        self._learning_watermark: dict[str, Any] = {}
        logger.info(
            "HubOrchestrator starting with model=%s, agents=%s",
            self._model,
            [spec.id for spec in self._registry],
        )
        logger.debug(
            "Agent specs: %s",
            [f"{spec.id}:{spec.runtime['mode']}" for spec in self._registry],
        )
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        store = get_knowledge_store()
        checkpointer = get_checkpointer()

        agent_tools = [_make_agent_tool(s) for s in self._registry]
        memory_tools = _build_memory_tools(store, self._registry)
        tools = agent_tools + memory_tools

        llm = ChatOpenAI(model=self._model, temperature=0)

        agent_names = [spec.id for spec in self._registry]
        logger.info(
            "Building LangGraph react agent with model=%s, tools=%s, memory_tools=%s",
            self._model,
            agent_names,
            ["shared_docs"],
        )
        logger.debug("Base system prompt length=%d chars", len(_SYSTEM_PROMPT))

        return create_react_agent(
            model=llm,
            tools=tools,
            prompt=_build_system_prompt,
            checkpointer=checkpointer,
            store=store,
        )

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def registry(self) -> list[AgentSpec]:
        return self._registry

    def _rotate_session(self, *, carry_active_work: bool) -> None:
        previous_session_id = self._session_id
        self._session_id = str(uuid.uuid4())
        persist_session_id(self._session_id)
        if carry_active_work:
            human_logger.info(
                "Started a new hub conversation. Future turns use a fresh LangGraph thread "
                "and clean session-scoped controls. Existing active work is unchanged."
            )
        else:
            human_logger.info(
                "Reset the hub conversation. Future turns use a fresh LangGraph thread "
                "and clean session-scoped controls."
            )
        logger.info(
            "Rotated hub session: old=%s new=%s carry_active_work=%s",
            previous_session_id,
            self._session_id,
            carry_active_work,
        )

    def new_session(self) -> str:
        self._rotate_session(carry_active_work=True)
        return self._hub_summary("New Agent Hub session")

    def reset_session(self) -> str:
        stop_reply = self.stop_current_task(reason="Reset by user")
        stopped_active_task = stop_reply != "No task is currently active."
        self._rotate_session(carry_active_work=False)
        if stopped_active_task:
            return "Reset complete. Stopped the active task and started a fresh conversation."
        return "Reset complete. Started a fresh conversation."

    def hub_status(self) -> str:
        """Report the same startup metadata as /new without rotating the session."""
        return self._hub_summary("Agent Hub status")

    def _hub_summary(self, heading: str) -> str:
        learning_enabled = get_learning_mode_registry().is_enabled(self._session_id)
        project = get_project_context_registry().get(self._session_id)
        project_label = project.metadata.get("name", project.root) if project else "none"
        active_run = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        active_label = "none" if active_run is None else active_run.state
        memory_count = len(HubMemoryManager().list_learnings())
        return "\n".join(
            [
                heading,
                "",
                f"Learning: {'ON' if learning_enabled else 'OFF'}",
                f"Project: {project_label}",
                f"Agents available: {len(self._registry)}",
                f"Active task: {active_label}",
                f"Memory records: {memory_count}",
            ]
        )

    def pending_run(self) -> TaskRun | None:
        project_key = _project_key_for_session(self._session_id)
        return get_task_run_store().get_latest_paused_run(
            self._session_id, project_key=project_key
        )

    def current_run_status(self) -> str:
        run = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        return format_current_run_status(run)

    def last_run_status(self) -> str:
        run = get_task_run_store().get_latest_completed_or_failed_run(self._session_id)
        return format_last_run_status(run)

    def learn(self, value: str, *, source: str, category: str | None = None) -> str:
        manager = HubMemoryManager()
        existing_operator = [
            r for r in manager.list_learnings() if r.scope == "operator" and r.status == "active"
        ]
        relevant_skills = self._skill_store.find_relevant_skills(value)
        relevant_docs = self._context_service.find_relevant_documentation(value)

        try:
            decision = self._learning_analyzer(
                value, existing_operator, relevant_skills, relevant_docs
            )
        except Exception as exc:
            logger.warning("Learning analysis failed for %r: %s", value, exc)
            record = manager.learn(value, source=source, category=category)
            return format_learning_confirmation(record, analysis_error=str(exc))

        if decision.memory_type == "procedural":
            record = manager.learn_procedural(value, source=source, category=category)
        else:
            record = manager.learn(value, source=source, category=category)

        skill_result = None
        if decision.action_kind == "skill":
            if decision.skill_slug and decision.skill_title and decision.skill_body:
                skill_result = self._skill_store.propose_skill(
                    decision.skill_slug,
                    decision.skill_title,
                    decision.skill_body,
                    source=f"learn:{record.identifier}",
                )
            else:
                skill_result = SkillProposalResult(
                    accepted=False,
                    skill=None,
                    reason=(
                        "Analysis chose 'skill' but did not provide a complete "
                        "slug/title/body."
                    ),
                )

        return format_learning_confirmation(
            record,
            analysis=decision,
            relevant_skills=relevant_skills,
            relevant_docs=relevant_docs,
            skill_result=skill_result,
        )

    def memory(self) -> str:
        return format_learning_list(HubMemoryManager().list_learnings())

    def set_learning_notifier(self, callback: Any) -> None:
        """Register how to surface a passive 'learned: X' FYI (e.g. Telegram send, print)."""
        self._learning_notify = callback

    def set_learning_mode(self, enabled: bool) -> str:
        get_learning_mode_registry().set_enabled(self._session_id, enabled)
        return f"Learning mode is now {'ON' if enabled else 'OFF'}."

    def learning_mode_status(self) -> str:
        enabled = get_learning_mode_registry().is_enabled(self._session_id)
        return f"Learning mode is {'ON' if enabled else 'OFF'}."

    def set_current_project(self, path: str) -> str:
        """Set the sticky project passed to subprocess specialists.

        Hub does not validate this against any specialist's allowlist —
        that check happens server-side in the specialist. Hub resolves the
        path to a canonical `ProjectContext` (project_id, contract version,
        fingerprint) and revalidates that context fresh immediately before
        every dispatch (see `ProjectContextRegistry.resolve_for_dispatch`),
        so a selection that later goes stale stops the dispatch instead of
        silently sending whatever the path used to resolve to.
        """
        try:
            context = get_project_context_registry().set(self._session_id, path)
        except ValueError as exc:
            human_logger.info("Project selection rejected for %r: %s", path, exc)
            return f"Error: {exc}"
        human_logger.info(
            "Session %s: current project set to %s (project_id=%s)",
            self._session_id[:8],
            context.root,
            context.project_id,
        )
        return f"Current project set to {context.root} (project_id: {context.project_id})."

    def clear_current_project(self) -> str:
        get_project_context_registry().clear(self._session_id)
        human_logger.info("Session %s: current project cleared", self._session_id[:8])
        return "Current project cleared — specialists will use their own default project."

    def current_project_status(self) -> str:
        current = get_project_context_registry().get(self._session_id)
        if current is None:
            return "No project selected — specialists use their own default project."
        return (
            f"Current project: {current.root} "
            f"(project_id: {current.project_id}, contract v{current.contract_version})."
        )

    def run_learning_pass(self, session_id: str) -> list[str]:
        """Deferred ('dreaming') pass: decide what from a now-quiet session is
        worth remembering automatically. See learning_mode.py for the debounced
        trigger; this method does the actual extraction + storage.
        """
        store = get_task_run_store()
        watermark = self._learning_watermark.get(session_id)
        runs = [
            r
            for r in store.list_runs(session_id=session_id)
            if r.state == TASK_STATE_SUCCEEDED and (watermark is None or r.created_at > watermark)
        ]
        if not runs:
            return []

        conversation_text = "\n\n".join(
            f"Human: {r.user_message}\nHub: {r.final_response or ''}" for r in runs
        )
        manager = HubMemoryManager()
        existing_semantic = manager.list_learnings(types=["semantic"])
        existing_active_auto = [
            r for r in existing_semantic if r.scope == "auto" and r.status == "active"
        ]
        existing_active_operator = [
            r for r in existing_semantic if r.scope == "operator" and r.status == "active"
        ]
        operator_ids = {r.identifier for r in existing_active_operator}

        try:
            candidates: list[ExtractionCandidate] = self._semantic_extractor(
                conversation_text, existing_active_auto, existing_active_operator
            )
        except Exception:
            logger.exception("Learning pass extraction failed for session %s", session_id)
            self._learning_watermark[session_id] = runs[-1].created_at
            return []

        messages: list[str] = []
        for candidate in candidates:
            if candidate.confidence != "high":
                logger.debug(
                    "Learning pass: skipping %s-confidence candidate: %s",
                    candidate.confidence,
                    _truncate(candidate.value),
                )
                continue
            # Rob's explicit /learn records are authoritative and can only be
            # changed by Rob doing that again — never let automatic extraction
            # disable one, even if the model proposed it.
            if candidate.supersedes_id and candidate.supersedes_id in operator_ids:
                logger.warning(
                    "Learning pass: refusing to auto-supersede operator record %s; "
                    "skipping candidate: %s",
                    candidate.supersedes_id,
                    _truncate(candidate.value),
                )
                continue
            if candidate.action == "update" and candidate.supersedes_id:
                manager.set_status(candidate.supersedes_id, "disabled")
            record = manager.record_auto_semantic(
                candidate.value,
                source=f"auto-extraction (session {session_id[:8]})",
                evidence=[r.id for r in runs],
            )
            human_logger.info("Learning pass: stored %s: %s", record.identifier, record.value)
            messages.append(f"\U0001f9e0 Learned: {record.value}")

        self._learning_watermark[session_id] = runs[-1].created_at
        return messages

    def _on_dream_fire(self, session_id: str) -> None:
        try:
            messages = self.run_learning_pass(session_id)
        except Exception:
            logger.exception("Dream pass failed for session %s", session_id)
            return
        if self._learning_notify is None:
            return
        for message in messages:
            try:
                self._learning_notify(message)
            except Exception:
                logger.exception("Learning notifier failed for session %s", session_id)

    def forget_learning(self, identifier: str) -> str:
        key = identifier.strip()
        if not key:
            return "Usage: /forget <memory identifier>"
        if not HubMemoryManager().forget(key):
            return f"No stored learning exists with identifier '{key}'."
        return format_forget_confirmation(key)

    def stop_current_task(self, reason: str = "Stopped by user") -> str:
        store = get_task_run_store()
        project_key = _project_key_for_session(self._session_id)
        run = store.get_latest_active_or_paused_run(self._session_id, project_key=project_key)
        if run is None:
            human_logger.info(
                "Stop requested for project '%s', but no active or paused task was found.",
                _friendly_project_label(project_key),
            )
            return "No task is currently active."

        agent_id = run.selected_agent_id or "unknown-agent"
        confirmation = f"Stopped run {run.id} for agent '{agent_id}'. State is now cancelled."
        _human_task_log(
            run.id,
            "Operator requested stop for agent '%s' in project '%s'.",
            agent_id,
            _friendly_project_label(project_key),
        )
        handle = get_task_control_registry().request_cancel(run.id, reason)

        current = store.get_run(run.id)
        if current is not None and not is_terminal_state(current.state):
            store.transition(
                run.id,
                TASK_STATE_CANCELLED,
                detail=f"Human cancelled the task: {reason}",
                selected_agent_id=run.selected_agent_id,
                final_response=confirmation,
                cancellation_reason=reason,
                raw_result={"status": "cancelled", "summary": reason},
            )
            _human_task_log(run.id, "Hub marked the task as cancelled.")
        if handle is not None:
            handle.mark_stop_reply_sent()
        return confirmation

    def approve_pending(self, *, progress_notify: Any | None = None) -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        spec = self._require_spec(pending)
        _human_task_log(pending.id, "Approval received. Resuming %s.", spec.name)
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="Human approved the pending task.",
            selected_agent_id=spec.id,
        )
        with active_task_run(pending.id, progress_callback=progress_notify):
            if spec.runtime["mode"] == "factory_brain":
                output = _dispatch_factory_brain(
                    spec,
                    pending.dispatched_task or pending.user_message,
                    thread_id=pending.context.get("agent_thread_id"),
                    action="resume",
                )
            else:
                output = _dispatch_subprocess(
                    spec,
                    pending.dispatched_task or pending.user_message,
                    human_approved=True,
                    approval_token=pending.approval_token,
                    request_id=pending.context.get("agent_request_id"),
                    project_root_override=pending.context.get("agent_dispatch_project_root"),
                    project_context_override=_project_context_override_from_pending(pending),
                    references=pending.context.get("agent_dispatch_references"),
                )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def reject_pending(self, reason: str = "Rejected by user") -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        spec = self._require_spec(pending)
        _human_task_log(pending.id, "Approval rejected. Stopping %s: %s", spec.name, reason)
        response_text: str
        raw_result: dict[str, Any]
        if spec.runtime["mode"] == "factory_brain" and pending.context.get("agent_thread_id"):
            result = reject_factory_request(
                working_directory=spec.runtime["working_directory"],
                thread_id=pending.context["agent_thread_id"],
                reason=reason,
            )
            response_text = f"[{spec.name}] {result.get('response', reason)}"
            raw_result = {"status": "failed", "summary": result.get("response", reason)}
        else:
            response_text = f"[{spec.name}] Request rejected: {reason}"
            raw_result = {"status": "failed", "summary": reason}

        get_task_run_store().transition(
            pending.id,
            TASK_STATE_FAILED,
            detail=f"Human rejected the pending task: {reason}",
            selected_agent_id=spec.id,
            final_response=response_text,
            error_message=reason,
            raw_result=raw_result,
        )
        return response_text

    def provide_clarification(
        self,
        clarification: str,
        *,
        progress_notify: Any | None = None,
    ) -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_CLARIFICATION:
            return self.invoke(clarification, progress_notify=progress_notify)

        spec = self._require_spec(pending)
        store = get_task_run_store()

        true_resume = spec.runtime["mode"] != "factory_brain" and bool(
            spec.interaction_contract.get("resume")
        )
        if true_resume and pending.context.get("specialist_resume_token") is None:
            # The specialist declared true resume support but Hub has no
            # resume state recorded for this pause — do not guess by falling
            # back to the reconstructed-task shape, which may not even be a
            # request a true-resume specialist knows how to interpret.
            message = (
                f"[{spec.name}] Cannot resume: no resume state was recorded "
                "for this paused task."
            )
            _human_task_log(
                pending.id,
                "Clarification received, but %s declares true resume support "
                "and no resume state was recorded. Failing clearly instead of "
                "guessing.",
                spec.name,
            )
            store.transition(
                pending.id,
                TASK_STATE_FAILED,
                detail="Specialist declares resume support but no resume state was recorded.",
                selected_agent_id=spec.id,
                final_response=message,
                error_message="Missing specialist_resume_token for a resume-capable specialist.",
            )
            return message

        _human_task_log(pending.id, "Clarification received. Resuming %s.", spec.name)
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="User provided clarification for the paused task.",
            selected_agent_id=spec.id,
        )
        with active_task_run(pending.id, progress_callback=progress_notify):
            if spec.runtime["mode"] == "factory_brain":
                output = _dispatch_factory_brain(
                    spec,
                    clarification,
                    thread_id=pending.context.get("agent_thread_id"),
                    action="invoke",
                )
            elif true_resume:
                output = _dispatch_subprocess(
                    spec,
                    clarification,
                    request_id=pending.context.get("agent_request_id"),
                    resume=pending.context.get("specialist_resume_token"),
                    project_root_override=pending.context.get("agent_dispatch_project_root"),
                    project_context_override=_project_context_override_from_pending(pending),
                    references=pending.context.get("agent_dispatch_references"),
                )
            else:
                resumed_task = (
                    f"{pending.dispatched_task or pending.user_message}\n\n"
                    f"Additional clarification from the user: {clarification}"
                )
                output = _dispatch_subprocess(
                    spec,
                    resumed_task,
                    request_id=pending.context.get("agent_request_id"),
                )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def provide_decision(
        self,
        option: str,
        text: str = "",
        *,
        actor: str = "human",
        progress_notify: Any | None = None,
    ) -> str:
        """Resume a task paused on a specialist's generic `pending_decision`.

        `option` must be one of the names the specialist itself last
        reported in `pending_decision.options` — Hub validates against that
        specialist-declared list, never against a fixed or specialist-
        specific set of names. The paused conversation resumes by
        resubmitting the same `request_id` alongside the decision; Hub does
        not need or use a separate resume token for this path.
        """
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_DECISION:
            return "No task is currently waiting for a decision."

        spec = self._require_spec(pending)
        pending_decision = pending.context.get("specialist_pending_decision") or {}
        options = pending_decision.get("options") or []
        allowed = {opt["name"] for opt in options if isinstance(opt, dict) and opt.get("name")}
        if option not in allowed:
            valid = ", ".join(sorted(allowed)) or "none"
            return f"[{spec.name}] '{option}' is not a valid option right now. Valid options: {valid}."

        _human_task_log(
            pending.id, "Decision '%s' received. Resuming %s.", option, spec.name
        )
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail=f"User provided decision '{option}' for the paused task.",
            selected_agent_id=spec.id,
        )
        decision = {"option": option, "text": text, "actor": actor}
        with active_task_run(pending.id, progress_callback=progress_notify):
            output = _dispatch_subprocess(
                spec,
                "",
                request_id=pending.context.get("agent_request_id"),
                decision=decision,
                project_root_override=pending.context.get("agent_dispatch_project_root"),
                project_context_override=_project_context_override_from_pending(pending),
                references=pending.context.get("agent_dispatch_references"),
            )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def _reconcile_registry(self) -> tuple[list[str], list[str], list[str]]:
        """Re-read Factory's registry and rebuild the tool graph if it changed.

        Runs before every turn (see `invoke`) so a specialist that Factory
        added, edited, or removed from `config/agents/` since Hub started
        takes effect on the next request — no Hub restart or Hub code
        change required. Also runs immediately on `/agents-refresh` (see
        `refresh_registry`) for an operator who doesn't want to wait for
        the next message. Registry health (invalid manifests) is refreshed
        on every call regardless of whether the callable agent set itself
        changed, so `/agents-refresh` and `/status`-adjacent health views
        stay current even on a no-op turn.
        """
        fresh = _load_specialists()
        self._registry_errors = _load_registry_errors()
        self._registry_last_refreshed = _utcnow_iso()
        if fresh == self._registry:
            return [], [], []

        previous_by_id = {spec.id: spec for spec in self._registry}
        fresh_by_id = {spec.id: spec for spec in fresh}
        added = sorted(fresh_by_id.keys() - previous_by_id.keys())
        removed = sorted(previous_by_id.keys() - fresh_by_id.keys())
        changed = sorted(
            agent_id
            for agent_id in fresh_by_id.keys() & previous_by_id.keys()
            if fresh_by_id[agent_id] != previous_by_id[agent_id]
        )

        self._registry = fresh
        self._graph = self._build_graph()

        human_logger.info(
            "Hub refreshed the agent registry — added: %s, changed: %s, removed: %s.",
            ", ".join(added) or "none",
            ", ".join(changed) or "none",
            ", ".join(removed) or "none",
        )
        logger.info(
            "Registry reconciliation rebuilt tools: added=%s changed=%s removed=%s agents=%s",
            added,
            changed,
            removed,
            [spec.id for spec in fresh],
        )
        return added, changed, removed

    def _format_registry_errors(self) -> list[str]:
        if not self._registry_errors:
            return ["Invalid manifests: none."]
        lines = [f"Invalid manifest(s) ({len(self._registry_errors)}):"]
        lines.extend(f"  - {err.source}: {err.message}" for err in self._registry_errors)
        return lines

    def refresh_registry(self) -> str:
        """Explicit, immediate registry refresh — the `/agents-refresh` command.

        `invoke()` already reconciles the registry before every turn
        (bounded to once per incoming message, per AGENT-HUB-040); this is
        for an operator who wants that to happen right now — e.g.
        immediately after staging a new specialist — and it surfaces
        registry health (invalid manifests skipped on load) that the
        per-turn reconciliation only logs.
        """
        added, changed, removed = self._reconcile_registry()
        lines = [
            f"Registry refreshed at {self._registry_last_refreshed} — "
            f"{len(self._registry)} agent(s) active.",
            f"Added: {', '.join(added) or 'none'}",
            f"Changed: {', '.join(changed) or 'none'}",
            f"Removed: {', '.join(removed) or 'none'}",
        ]
        lines.extend(self._format_registry_errors())
        return "\n".join(lines)

    def agents_status(self) -> str:
        """Read-only registry health view — the `/agents-status` command.

        Unlike `/agents-refresh`, this never re-reads the registry from
        disk; it reports state as of the last reconciliation (per-turn or
        explicit), each agent's pinned-identity fields (version and
        manifest fingerprint — the same fingerprint a paused task pins
        against, see `_require_spec`), and any invalid manifest from that
        last read. Safe to call with no side effects.
        """
        lines = [
            f"Registry last refreshed at {self._registry_last_refreshed} — "
            f"{len(self._registry)} agent(s) active.",
        ]
        if self._registry:
            lines.extend(
                f"  - {spec.id} (v{spec.version}, fingerprint {spec_fingerprint(spec)[:8]})"
                for spec in self._registry
            )
        else:
            lines.append("  (none)")
        lines.extend(self._format_registry_errors())
        return "\n".join(lines)

    def invoke(self, message: str, *, progress_notify: Any | None = None) -> str:
        logger.info("Received user request: %s", message)
        logger.debug("Invoking graph with session_id=%s", self._session_id)
        self._reconcile_registry()
        task_store = get_task_run_store()

        project_key = _project_key_for_session(self._session_id)
        busy_run = task_store.get_active_or_paused_run_for_project(project_key)
        if busy_run is not None:
            human_logger.info(
                "Rejected new task for project '%s' — run %s is still %s",
                _friendly_project_label(project_key),
                busy_run.id[:8],
                busy_run.state,
            )
            return (
                f"A task is already running for project '{project_key}'. "
                "Use /status or /stop before sending another request for that project."
            )

        task_run = task_store.create_run(session_id=self._session_id, user_message=message)
        task_store.update_run(task_run.id, context_updates={"target_project": project_key})
        _human_task_log(
            task_run.id,
            "Hub is deciding how to handle this request for project '%s'.",
            _friendly_project_label(project_key),
        )
        thread_id = f"{self._session_id}:{project_key}"
        logger.debug("Task %s: project=%s thread_id=%s", task_run.id[:8], project_key, thread_id)
        _repair_dangling_tool_calls(
            self._graph, thread_id, "Interrupted before the specialist could reply."
        )
        config = {"configurable": {"thread_id": thread_id}}
        usage_cb = UsageMetadataCallbackHandler()
        run_config = {**config, "callbacks": [usage_cb]}
        started_at = time.perf_counter()
        get_task_control_registry().register_run(task_run.id)
        graph_path: list[str] = []
        graph_steps: list[tuple[str, str]] = []
        graph_trace_emitted = False
        try:
            with active_task_run(task_run.id, progress_callback=progress_notify):
                if hasattr(self._graph, "stream"):
                    result = None
                    last_node = None
                    for event in self._graph.stream(
                        {"messages": [HumanMessage(content=message)]},
                        config=run_config,
                        stream_mode=["tasks", "updates", "values"],
                    ):
                        last_node, streamed_values = _consume_graph_stream_event(
                            task_run.id, event, last_node, graph_path, graph_steps
                        )
                        if streamed_values is not None:
                            result = streamed_values
                    if result is None:
                        raise RuntimeError("Orchestrator stream returned no final state.")
                else:
                    result = self._graph.invoke(
                        {"messages": [HumanMessage(content=message)]},
                        config=run_config,
                    )
            messages = result.get("messages", [])
            if not messages:
                raise RuntimeError("Orchestrator returned no messages.")

            graph_trace = _render_graph_trace(graph_path, graph_steps)
            if graph_trace:
                logger.debug("Task %s: %s", task_run.id, graph_trace)
                graph_trace_emitted = True

            reply = _relay_specialist_terminal_message(messages) or messages[-1].content
            run_record = _record_orchestrator_llm_run(
                requested_model=self._model,
                effective_model=self._model,
                duration_seconds=time.perf_counter() - started_at,
                usage_cb=usage_cb,
                thread_id=self._session_id,
                result_preview=reply,
            )
            current = task_store.get_run(task_run.id)
            if current is None:
                raise RuntimeError(f"Task run disappeared: {task_run.id}")

            if is_active_state(current.state):
                _human_task_log(task_run.id, "Hub has a final answer ready for the operator.")
                task_store.transition(
                    task_run.id,
                    TASK_STATE_SUCCEEDED,
                    detail="Hub returned a final reply to the user.",
                    final_response=reply,
                    requested_model=run_record["requested_model"],
                    effective_model=run_record["effective_model"],
                    duration_ms=run_record["duration_ms"],
                    usage={"totals": run_record["totals"], "models": run_record["usage"]},
                    cost=run_record["cost"],
                )
            elif is_paused_state(current.state):
                task_store.update_run(
                    task_run.id,
                    final_response=reply,
                    requested_model=run_record["requested_model"],
                    effective_model=run_record["effective_model"],
                    duration_ms=run_record["duration_ms"],
                    usage={"totals": run_record["totals"], "models": run_record["usage"]},
                    cost=run_record["cost"],
                )
                get_learning_mode_registry().notify_task_completed(
                    self._session_id, self._on_dream_fire
                )

            return reply
        except TaskCancelled:
            graph_trace = _render_graph_trace(graph_path, graph_steps)
            if graph_trace and not graph_trace_emitted:
                logger.debug("Task %s: %s", task_run.id, graph_trace)
            current = task_store.get_run(task_run.id)
            if current is not None and current.state == TASK_STATE_CANCELLED:
                raise
            _human_task_log(task_run.id, "The task was cancelled while work was in progress.")
            task_store.transition(
                task_run.id,
                TASK_STATE_CANCELLED,
                detail="Task cancelled during specialist execution.",
                cancellation_reason="Stopped by user",
                raw_result={"status": "cancelled", "summary": "Stopped by user"},
            )
            raise
        except Exception as exc:
            graph_trace = _render_graph_trace(graph_path, graph_steps)
            if graph_trace and not graph_trace_emitted:
                logger.debug("Task %s: %s", task_run.id, graph_trace)
            _human_task_log(task_run.id, "Hub orchestration failed: %s", exc)
            run_record = _record_orchestrator_llm_run(
                requested_model=self._model,
                effective_model=self._model,
                duration_seconds=time.perf_counter() - started_at,
                usage_cb=usage_cb,
                thread_id=self._session_id,
                status="error",
                error=str(exc),
            )
            current = task_store.get_run(task_run.id)
            if current is not None and not is_terminal_state(current.state):
                task_store.transition(
                    task_run.id,
                    TASK_STATE_FAILED,
                    detail=f"Hub orchestration failed: {exc}",
                    error_message=str(exc),
                    requested_model=run_record["requested_model"],
                    effective_model=run_record["effective_model"],
                    duration_ms=run_record["duration_ms"],
                    usage={"totals": run_record["totals"], "models": run_record["usage"]},
                    cost=run_record["cost"],
                )
            raise
        finally:
            get_task_control_registry().unregister_run(task_run.id)

    def _finalize_specialist_follow_up(self, run_id: str, spec: AgentSpec, output: dict) -> str:
        reply = _format_output(spec, output)
        store = get_task_run_store()
        current = store.get_run(run_id)
        if current is None:
            raise RuntimeError(f"Task run disappeared: {run_id}")
        if is_active_state(current.state):
            _human_task_log(run_id, "Hub is sending %s's reply back to the operator.", spec.name)
            store.transition(
                run_id,
                TASK_STATE_SUCCEEDED,
                detail="Hub returned the specialist follow-up reply to the user.",
                final_response=reply,
            )
            get_learning_mode_registry().notify_task_completed(
                self._session_id, self._on_dream_fire
            )
        elif is_paused_state(current.state):
            store.update_run(run_id, final_response=reply)
        return reply

    def _require_spec(self, pending: TaskRun) -> AgentSpec:
        """Return the agent spec a paused task should resume against.

        A dispatch pins the exact spec it used into the task's context (see
        `pinned_agent_spec` in `_dispatch_subprocess`/`_dispatch_factory_brain`).
        Resume always prefers that pinned snapshot over the live registry —
        if Factory changed or removed this agent while the task was paused,
        resume still uses the manifest version the task was actually
        dispatched against (AGENT-HUB-040), rather than silently picking up
        different behavior or failing just because the id moved. Paused
        tasks from before this pinning existed have no `pinned_agent_spec`
        and fall back to a live-registry lookup by id, as before.
        """
        agent_id = pending.selected_agent_id
        if not agent_id:
            raise RuntimeError("Paused task has no selected agent.")

        pinned_data = pending.context.get("pinned_agent_spec")
        if isinstance(pinned_data, dict):
            try:
                pinned_spec = AgentSpec(**pinned_data)
            except TypeError:
                logger.warning(
                    "Task %s: pinned_agent_spec for '%s' could not be reconstructed; "
                    "falling back to the live registry.",
                    pending.id,
                    agent_id,
                )
                pinned_spec = None
            if pinned_spec is not None:
                live = next((spec for spec in self._registry if spec.id == agent_id), None)
                if live is None:
                    logger.info(
                        "Task %s: resuming '%s' (version=%s) pinned to its dispatch-time "
                        "manifest; the live registry no longer has this agent.",
                        pending.id,
                        agent_id,
                        pinned_spec.version,
                    )
                elif spec_fingerprint(live) != pending.context.get("pinned_agent_fingerprint"):
                    logger.info(
                        "Task %s: resuming '%s' (version=%s) pinned to its dispatch-time "
                        "manifest; the live registry's manifest for this agent has since "
                        "changed.",
                        pending.id,
                        agent_id,
                        pinned_spec.version,
                    )
                return pinned_spec

        for spec in self._registry:
            if spec.id == agent_id:
                return spec
        raise RuntimeError(f"Selected agent is no longer registered: {agent_id}")


def cancel_all_active_tasks(reason: str) -> list[str]:
    """Terminate every in-flight specialist subprocess and mark its run cancelled.

    Call this on graceful shutdown (Ctrl-C, SIGTERM). Without it, stopping the
    Hub process leaves any dispatched specialist subprocess running headless
    in its own process group (see subprocess_popen_kwargs) with no parent left
    to record its result, and the task-run row stuck at in_progress forever.
    Paused runs (waiting on approval/clarification) have no live process
    attached and are intentionally left alone — they are durable and resume
    normally after a restart.
    """
    registry = get_task_control_registry()
    store = get_task_run_store()
    cancelled_run_ids: list[str] = []
    for run_id in registry.list_active_run_ids():
        registry.request_cancel(run_id, reason)
        current = store.get_run(run_id)
        if current is None or is_terminal_state(current.state):
            continue
        store.transition(
            run_id,
            TASK_STATE_CANCELLED,
            detail=f"Hub shutdown cancelled the task: {reason}",
            selected_agent_id=current.selected_agent_id,
            final_response=f"Cancelled: {reason}",
            cancellation_reason=reason,
            raw_result={"status": "cancelled", "summary": reason},
        )
        _human_task_log(run_id, "Hub marked the task as cancelled due to shutdown.")
        cancelled_run_ids.append(run_id)
    return cancelled_run_ids


def _load_specialists() -> list[AgentSpec]:
    registry = load_registry_report().specs
    factory_spec = build_factory_agent_spec()
    if factory_spec is not None and not any(spec.id == factory_spec.id for spec in registry):
        registry.append(factory_spec)
    return registry


def _load_registry_errors() -> list[RegistryLoadError]:
    """Agent.json files the last real registry read couldn't parse.

    Read independently of `_load_specialists` (which many tests monkeypatch
    with a fixed fake list) so it always reflects the real
    `AGENT_REGISTRY_DIR` on disk — used only for `/agents-refresh` health
    reporting, never for building the callable tool set.
    """
    return load_registry_report().errors


def _record_orchestrator_llm_run(
    *,
    requested_model: str | None,
    effective_model: str | None,
    duration_seconds: float,
    usage_cb: Any,
    thread_id: str | None,
    status: str = "ok",
    error: str | None = None,
    result_preview: str | None = None,
) -> dict[str, Any]:
    return record_llm_run(
        operation="hub_orchestrator_invoke",
        request_kind="thread-turn",
        requested_model=requested_model or effective_model,
        effective_model=effective_model,
        status=status,
        duration_seconds=duration_seconds,
        usage_by_model=extract_usage_metadata(usage_cb),
        error=error,
        thread_id=thread_id,
        result_preview=result_preview,
    )
