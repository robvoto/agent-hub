"""Bounded LangGraph-native fan-out/join for specialist tasks."""

from __future__ import annotations

import json
import logging
import operator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from .config import CONFIG_DIR
from .project_context import ProjectContext, get_project_context_registry
from .registry import AgentSpec
from .task_control import TaskCancelled, get_task_control_registry
from .task_runs import (
    TASK_STATE_CANCELLED,
    TASK_STATE_FAILED,
    TASK_STATE_SUCCEEDED,
    active_task_run,
    get_task_run_store,
    is_active_state,
    is_terminal_state,
)

logger = logging.getLogger(__name__)
FANOUT_CONFIG_FILE = CONFIG_DIR / "fanout.json"


@dataclass(frozen=True)
class FanoutConfig:
    max_branches: int
    max_concurrency: int


class FanoutError(RuntimeError):
    """Raised when a fan-out request violates a bounded orchestration rule."""


class _State(TypedDict):
    branches: list[dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]


class _BranchState(TypedDict):
    index: int
    child_run_id: str
    agent_id: str
    task_kind: str
    task: str
    project: ProjectContext


def load_fanout_config(path: Path = FANOUT_CONFIG_FILE) -> FanoutConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FanoutError(f"Fan-out config could not be loaded: {path}: {exc}") from exc
    max_branches = int(raw.get("max_branches", 0))
    max_concurrency = int(raw.get("max_concurrency", 0))
    if not 2 <= max_branches <= 8:
        raise FanoutError("fanout.max_branches must be between 2 and 8.")
    if not 1 <= max_concurrency <= max_branches:
        raise FanoutError("fanout.max_concurrency must be between 1 and max_branches.")
    return FanoutConfig(max_branches=max_branches, max_concurrency=max_concurrency)


def run_specialist_fanout(
    *,
    session_id: str,
    parent_run_id: str,
    registry: list[AgentSpec],
    branches: list[dict[str, str]],
    dispatch: Callable[[AgentSpec, str, str, ProjectContext], dict[str, Any]],
    format_output: Callable[[AgentSpec, dict[str, Any]], str],
    config: FanoutConfig | None = None,
) -> list[dict[str, Any]]:
    settings = config or load_fanout_config()
    if len(branches) < 2:
        raise FanoutError("Fan-out requires at least 2 branches.")
    if len(branches) > settings.max_branches:
        raise FanoutError(
            f"Fan-out requested {len(branches)} branches; maximum is {settings.max_branches}."
        )

    specs = {spec.id: spec for spec in registry}
    project_registry = get_project_context_registry()
    store = get_task_run_store()
    resolved_branches: list[dict[str, Any]] = []
    project_ids: set[str] = set()

    for index, branch in enumerate(branches, start=1):
        agent_id = str(branch.get("agent_id") or "").strip()
        task_kind = str(branch.get("task_kind") or "").strip()
        task = str(branch.get("task") or "").strip()
        project_ref = str(branch.get("project") or "").strip()
        if not all((agent_id, task_kind, task, project_ref)):
            raise FanoutError(
                f"Branch {index} requires agent_id, task_kind, task, and project."
            )
        spec = specs.get(agent_id)
        if spec is None:
            raise FanoutError(f"Branch {index} names unknown specialist '{agent_id}'.")
        advertised = set(spec.task_contract.get("task_kinds", []) or [])
        if task_kind not in advertised:
            raise FanoutError(
                f"Branch {index} task_kind '{task_kind}' is not advertised by {agent_id}."
            )
        resolution = project_registry.resolve_known(project_ref)
        if resolution.error or resolution.context is None:
            raise FanoutError(f"Branch {index}: {resolution.error or 'project unavailable'}")
        project = resolution.context
        if project.project_id in project_ids:
            raise FanoutError(
                "Parallel branches must target distinct projects. "
                f"Project '{project.project_id}' appears more than once."
            )
        busy = store.get_active_or_paused_run_for_project(
            project.project_id,
            exclude_run_id=parent_run_id,
        )
        if busy is not None:
            raise FanoutError(
                f"Project '{project.project_id}' already has active run {busy.id[:8]}."
            )
        project_ids.add(project.project_id)
        resolved_branches.append(
            {
                "index": index,
                "agent_id": agent_id,
                "task_kind": task_kind,
                "task": task,
                "project": project,
            }
        )

    prepared: list[dict[str, Any]] = []
    try:
        for branch in resolved_branches:
            child = store.create_run(session_id=session_id, user_message=branch["task"])
            store.update_run(
                child.id,
                context_updates={
                    "target_project": branch["project"].project_id,
                    "fanout_parent_run_id": parent_run_id,
                    "fanout_branch_index": branch["index"],
                    "fanout_project_root": branch["project"].root,
                },
            )
            prepared.append({**branch, "child_run_id": child.id})
    except Exception:
        for branch in prepared:
            child = store.get_run(branch["child_run_id"])
            if child is not None and not is_terminal_state(child.state):
                store.transition(
                    child.id,
                    TASK_STATE_CANCELLED,
                    detail="Fan-out preparation failed before execution.",
                    cancellation_reason="Fan-out preparation failed",
                )
        raise

    store.update_context(
        parent_run_id,
        fanout_child_ids=[branch["child_run_id"] for branch in prepared],
        fanout_branch_count=len(prepared),
    )

    def route(state: _State) -> list[Send]:
        return [Send("branch", branch) for branch in state["branches"]]

    def execute_branch(state: _BranchState) -> dict[str, list[dict[str, Any]]]:
        child_id = state["child_run_id"]
        spec = specs[state["agent_id"]]
        control = get_task_control_registry()
        control.register_run(child_id)
        try:
            with active_task_run(child_id):
                output = dispatch(spec, state["task"], state["task_kind"], state["project"])
            current = store.get_run(child_id)
            if current is None:
                raise RuntimeError(f"Fan-out child run disappeared: {child_id}")
            response = format_output(spec, output)
            if is_active_state(current.state):
                store.transition(
                    child_id,
                    TASK_STATE_SUCCEEDED,
                    detail="Fan-out branch completed.",
                    selected_agent_id=spec.id,
                    final_response=response,
                )
                current = store.get_run(child_id)
                assert current is not None
            return {
                "results": [
                    {
                        "index": state["index"],
                        "child_run_id": child_id,
                        "agent_id": spec.id,
                        "project_id": state["project"].project_id,
                        "state": current.state,
                        "response": response,
                    }
                ]
            }
        except TaskCancelled as exc:
            current = store.get_run(child_id)
            if current is not None and not is_terminal_state(current.state):
                store.transition(
                    child_id,
                    TASK_STATE_CANCELLED,
                    detail=f"Fan-out branch cancelled: {exc.reason}",
                    selected_agent_id=spec.id,
                    cancellation_reason=exc.reason,
                )
            return {
                "results": [
                    {
                        "index": state["index"],
                        "child_run_id": child_id,
                        "agent_id": spec.id,
                        "project_id": state["project"].project_id,
                        "state": TASK_STATE_CANCELLED,
                        "response": exc.reason,
                    }
                ]
            }
        except Exception as exc:
            logger.exception("Fan-out branch %s failed", state["index"])
            current = store.get_run(child_id)
            if current is not None and not is_terminal_state(current.state):
                store.transition(
                    child_id,
                    TASK_STATE_FAILED,
                    detail=f"Fan-out branch failed: {exc}",
                    selected_agent_id=spec.id,
                    error_message=str(exc),
                )
            return {
                "results": [
                    {
                        "index": state["index"],
                        "child_run_id": child_id,
                        "agent_id": spec.id,
                        "project_id": state["project"].project_id,
                        "state": TASK_STATE_FAILED,
                        "response": str(exc),
                    }
                ]
            }
        finally:
            control.unregister_run(child_id)

    builder = StateGraph(_State)
    builder.add_node("branch", execute_branch, input_schema=_BranchState)
    builder.add_conditional_edges(START, route, ["branch"])
    builder.add_edge("branch", END)
    graph = builder.compile()
    result = graph.invoke(
        {"branches": prepared, "results": []},
        {"max_concurrency": settings.max_concurrency},
    )
    return sorted(result["results"], key=lambda item: item["index"])

