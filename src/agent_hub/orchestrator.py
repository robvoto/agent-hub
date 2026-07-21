"""LangGraph orchestrator — routes tasks to specialist agents via subprocess dispatch."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
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
from .hub_memory import (
    HubMemoryManager,
    format_forget_confirmation,
    format_learning_confirmation,
    format_learning_list,
    format_learnings_for_prompt,
)
from .knowledge_store import get_knowledge_store
from .log_config import get_human_logger
from .manifest_cache import get_manifest_cache
from .registry import AgentSpec, load_registry
from .run_status import format_current_run_status, format_last_run_status
from .shared_docs import make_shared_docs_tool
from .task_control import TaskCancelled, get_task_control_registry
from .task_runs import (
    TASK_STATE_CANCELLED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL,
    TASK_STATE_WAITING_CLARIFICATION,
    TaskRun,
    active_task_run,
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

_SYSTEM_PROMPT = """You are the Agent Hub orchestrator. You coordinate specialist AI agents.

When a user sends a request:
1. Select the correct agent from your tool list based on their purpose.
2. Call that agent's tool with a clear, bounded task description.
3. Return the agent's result to the user.

Do not explain what you would do — invoke the agent and return the result.
Do not answer coding, research, or creation tasks yourself — that is the specialist agent's job.

If the agent returns a clarification question, relay it to the user verbatim.
If the agent requires approval, tell the user exactly what needs approval and wait.
If no registered agent fits the task, say so clearly — do not invent capabilities."""


def _build_system_prompt(state: Any) -> list[Any]:
    """Assemble the system prompt fresh per turn, folding in operator-stored learnings.

    Reading the knowledge store on every model call (rather than baking learnings
    into the prompt at graph-construction time) means a /learn or /forget takes
    effect on the very next turn without restarting the hub.
    """
    messages = state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    learnings_block = format_learnings_for_prompt(HubMemoryManager().list_learnings())
    content = f"{_SYSTEM_PROMPT}\n\n{learnings_block}" if learnings_block else _SYSTEM_PROMPT
    return [SystemMessage(content=content)] + list(messages)


def _dispatch_subprocess(
    spec: AgentSpec,
    task: str,
    *,
    human_approved: bool = False,
    approval_token: str | None = None,
    request_id: str | None = None,
) -> dict:
    """Invoke a subprocess specialist and return its structured JSON output."""
    runtime = spec.runtime
    entrypoint = runtime["entrypoint"]
    working_dir = runtime["working_directory"]

    request_id = request_id or str(uuid.uuid4())
    task_run_id = get_current_task_run_id()
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
            },
        )
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_IN_PROGRESS,
            detail=f"Specialist agent '{spec.id}' is running.",
            selected_agent_id=spec.id,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "input.json"
        output_file = Path(tmpdir) / "output.json"

        input_data = {
            "request_id": request_id,
            "task": task,
            "source": "agent-hub",
            "execution_mode": runtime["default_execution_mode"],
        }
        if human_approved:
            input_data["human_approved"] = True
            if approval_token:
                input_data["approval_token"] = approval_token
        input_file.write_text(json.dumps(input_data, indent=2), encoding="utf-8")

        input_arg = runtime["input_arg"]
        output_arg = runtime["output_arg"]
        cmd = entrypoint.split() + [input_arg, str(input_file), output_arg, str(output_file)]

        human_logger.info("Running module: %s — %s", spec.id, _truncate(task))
        logger.info("Dispatching to %s (request_id=%s): %s", spec.id, request_id, task[:120])
        handle = get_task_control_registry().get_handle(task_run_id)
        if handle is not None and handle.cancel_requested:
            raise TaskCancelled(handle.cancellation_reason or "Stopped by user")

        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if task_run_id is not None:
            get_task_control_registry().attach_process(task_run_id, proc, agent_id=spec.id)
        try:
            while proc.poll() is None:
                time.sleep(0.05)
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
        human_logger.info(
            "Module %s finished (status=%s)", spec.id, output.get("status", "unknown")
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
            },
        )
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_IN_PROGRESS,
            detail=f"Specialist agent '{spec.id}' is running.",
            selected_agent_id=spec.id,
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
    _record_agent_status(spec, output, task_run_id)
    return output


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

    raise RuntimeError(
        f"Agent '{spec.id}' returned status '{status}': {summary or output}"
    )


def _record_agent_status(spec: AgentSpec, output: dict, task_run_id: str | None) -> None:
    if not task_run_id:
        return

    store = get_task_run_store()
    status = output.get("status", "unknown")
    summary = output.get("summary", "")

    if status == "needs_clarification":
        store.transition(
            task_run_id,
            TASK_STATE_WAITING_CLARIFICATION,
            detail=summary or f"Agent '{spec.id}' requested clarification.",
            selected_agent_id=spec.id,
        )
    elif status == "approval_required":
        store.transition(
            task_run_id,
            TASK_STATE_WAITING_APPROVAL,
            detail=summary or f"Agent '{spec.id}' requested approval.",
            selected_agent_id=spec.id,
            approval_token=output.get("approval_token"),
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
    def _call_agent(task: str) -> str:
        task_run_id = get_current_task_run_id()
        if task_run_id:
            get_task_run_store().transition(
                task_run_id,
                TASK_STATE_ROUTED,
                detail=f"Hub routed task to specialist agent '{spec.id}'.",
                selected_agent_id=spec.id,
            )
        if mode == "subprocess":
            return _format_output(spec, _dispatch_subprocess(spec, task))
        if mode == "factory_brain":
            return _format_output(spec, _dispatch_factory_brain(spec, task))
        raise RuntimeError(f"Unsupported runtime mode: {mode}")

    return _call_agent


def _build_memory_tools(store: Any) -> list[Any]:
    return [make_shared_docs_tool()]


class HubOrchestrator:
    """Stateful orchestrator with per-session thread isolation."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model
        self._registry = _load_specialists()
        self._session_id = str(uuid.uuid4())
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
        memory_tools = _build_memory_tools(store)
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

    def new_session(self) -> None:
        self._session_id = str(uuid.uuid4())
        logger.info("New hub session: %s", self._session_id)

    def pending_run(self) -> TaskRun | None:
        return get_task_run_store().get_latest_paused_run(self._session_id)

    def current_run_status(self) -> str:
        run = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        return format_current_run_status(run)

    def last_run_status(self) -> str:
        run = get_task_run_store().get_latest_completed_or_failed_run(self._session_id)
        return format_last_run_status(run)

    def learn(self, value: str, *, source: str, category: str | None = None) -> str:
        record = HubMemoryManager().learn(value, source=source, category=category)
        return format_learning_confirmation(record)

    def memory(self) -> str:
        return format_learning_list(HubMemoryManager().list_learnings())

    def forget_learning(self, identifier: str) -> str:
        key = identifier.strip()
        if not key:
            return "Usage: /forget <memory identifier>"
        if not HubMemoryManager().forget(key):
            return f"No stored learning exists with identifier '{key}'."
        return format_forget_confirmation(key)

    def stop_current_task(self, reason: str = "Stopped by user") -> str:
        store = get_task_run_store()
        run = store.get_latest_active_or_paused_run(self._session_id)
        if run is None:
            return "No task is currently active."

        agent_id = run.selected_agent_id or "unknown-agent"
        confirmation = f"Stopped run {run.id} for agent '{agent_id}'. State is now cancelled."
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
        if handle is not None:
            handle.mark_stop_reply_sent()
        return confirmation

    def approve_pending(self) -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        spec = self._require_spec(pending.selected_agent_id)
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="Human approved the pending task.",
            selected_agent_id=spec.id,
        )
        with active_task_run(pending.id):
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
                )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def reject_pending(self, reason: str = "Rejected by user") -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        spec = self._require_spec(pending.selected_agent_id)
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

    def provide_clarification(self, clarification: str) -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_CLARIFICATION:
            return self.invoke(clarification)

        spec = self._require_spec(pending.selected_agent_id)
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="User provided clarification for the paused task.",
            selected_agent_id=spec.id,
        )
        with active_task_run(pending.id):
            if spec.runtime["mode"] == "factory_brain":
                output = _dispatch_factory_brain(
                    spec,
                    clarification,
                    thread_id=pending.context.get("agent_thread_id"),
                    action="invoke",
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

    def invoke(self, message: str) -> str:
        logger.info("Received user request: %s", message)
        logger.debug("Invoking graph with session_id=%s", self._session_id)
        task_store = get_task_run_store()
        task_run = task_store.create_run(session_id=self._session_id, user_message=message)
        config = {"configurable": {"thread_id": self._session_id}}
        usage_cb = UsageMetadataCallbackHandler()
        run_config = {**config, "callbacks": [usage_cb]}
        started_at = time.perf_counter()
        get_task_control_registry().register_run(task_run.id)
        try:
            with active_task_run(task_run.id):
                result = self._graph.invoke(
                    {"messages": [HumanMessage(content=message)]},
                    config=run_config,
                )
            messages = result.get("messages", [])
            if not messages:
                raise RuntimeError("Orchestrator returned no messages.")

            reply = messages[-1].content
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

            return reply
        except TaskCancelled:
            current = task_store.get_run(task_run.id)
            if current is not None and current.state == TASK_STATE_CANCELLED:
                raise
            task_store.transition(
                task_run.id,
                TASK_STATE_CANCELLED,
                detail="Task cancelled during specialist execution.",
                cancellation_reason="Stopped by user",
                raw_result={"status": "cancelled", "summary": "Stopped by user"},
            )
            raise
        except Exception as exc:
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
            store.transition(
                run_id,
                TASK_STATE_SUCCEEDED,
                detail="Hub returned the specialist follow-up reply to the user.",
                final_response=reply,
            )
        elif is_paused_state(current.state):
            store.update_run(run_id, final_response=reply)
        return reply

    def _require_spec(self, agent_id: str | None) -> AgentSpec:
        if not agent_id:
            raise RuntimeError("Paused task has no selected agent.")
        for spec in self._registry:
            if spec.id == agent_id:
                return spec
        raise RuntimeError(f"Selected agent is no longer registered: {agent_id}")


def _load_specialists() -> list[AgentSpec]:
    registry = load_registry()
    factory_spec = build_factory_agent_spec()
    if factory_spec is not None and not any(spec.id == factory_spec.id for spec in registry):
        registry.append(factory_spec)
    return registry


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
