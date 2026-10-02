from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_hub.project_context import ProjectContext, ProjectContextResolution
from agent_hub.registry import AgentSpec
from agent_hub.specialist_fanout import FanoutConfig, FanoutError, run_specialist_fanout
from agent_hub.task_runs import (
    TASK_STATE_WAITING_APPROVAL,
    get_current_task_run_id,
    get_task_run_store,
)


def _spec(agent_id: str = "worker") -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        name=agent_id,
        purpose="test",
        task_contract={"task_kinds": ["analysis"]},
        runtime={
            "mode": "subprocess",
            "entrypoint": "stub",
            "working_directory": "/tmp",
            "input_arg": "--input",
            "output_arg": "--output",
            "default_execution_mode": "execute",
        },
    )


def _context(name: str) -> ProjectContext:
    return ProjectContext(
        project_id=f"project:{name}",
        root=f"/repo/{name}",
        contract_version=1,
        fingerprint=f"fp-{name}",
        metadata={"name": name},
    )


def test_fanout_runs_distinct_projects_and_persists_children(monkeypatch):
    contexts = {"a": _context("a"), "b": _context("b")}
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=contexts[value], error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    store = get_task_run_store()
    parent = store.create_run(session_id="session", user_message="fan out")
    store.update_run(parent.id, context_updates={"target_project": "parent"})
    calls = []

    def dispatch(spec, task, task_kind, project):
        calls.append((spec.id, task, task_kind, project.project_id))
        return {"status": "success", "summary": f"done {project.project_id}"}

    results = run_specialist_fanout(
        session_id="session",
        parent_run_id=parent.id,
        registry=[_spec()],
        branches=[
            {"agent_id": "worker", "task_kind": "analysis", "task": "one", "project": "a"},
            {"agent_id": "worker", "task_kind": "analysis", "task": "two", "project": "b"},
        ],
        dispatch=dispatch,
        format_output=lambda spec, output: output["summary"],
        config=FanoutConfig(max_branches=4, max_concurrency=2),
    )

    assert len(calls) == 2
    assert {result["state"] for result in results} == {"succeeded"}
    updated_parent = store.get_run(parent.id)
    assert updated_parent is not None
    assert len(updated_parent.context["fanout_child_ids"]) == 2


def test_fanout_rejects_same_project(monkeypatch):
    context = _context("a")
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=context, error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    parent = get_task_run_store().create_run(session_id="session", user_message="fan out")

    with pytest.raises(FanoutError, match="distinct projects"):
        run_specialist_fanout(
            session_id="session",
            parent_run_id=parent.id,
            registry=[_spec()],
            branches=[
                {"agent_id": "worker", "task_kind": "analysis", "task": "one", "project": "a"},
                {"agent_id": "worker", "task_kind": "analysis", "task": "two", "project": "a"},
            ],
            dispatch=lambda *args: {"status": "success", "summary": "done"},
            format_output=lambda spec, output: output["summary"],
            config=FanoutConfig(max_branches=4, max_concurrency=2),
        )


def test_fanout_validation_failure_creates_no_children(monkeypatch):
    context = _context("a")

    def resolve(value):
        if value == "a":
            return ProjectContextResolution(context=context, error=None)
        return ProjectContextResolution(context=None, error="unknown")

    fake_registry = SimpleNamespace(resolve_known=resolve)
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    store = get_task_run_store()
    parent = store.create_run(session_id="session", user_message="fan out")

    with pytest.raises(FanoutError, match="unknown"):
        run_specialist_fanout(
            session_id="session",
            parent_run_id=parent.id,
            registry=[_spec()],
            branches=[
                {"agent_id": "worker", "task_kind": "analysis", "task": "one", "project": "a"},
                {
                    "agent_id": "worker",
                    "task_kind": "analysis",
                    "task": "two",
                    "project": "missing",
                },
            ],
            dispatch=lambda *args: {"status": "success", "summary": "done"},
            format_output=lambda spec, output: output["summary"],
            config=FanoutConfig(max_branches=4, max_concurrency=2),
        )

    children = [
        run
        for run in store.list_runs(session_id="session")
        if run.context.get("fanout_parent_run_id") == parent.id
    ]
    assert children == []


def test_fanout_rejects_more_than_configured_maximum(monkeypatch):
    contexts = {name: _context(name) for name in ("a", "b", "c")}
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=contexts[value], error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    parent = get_task_run_store().create_run(session_id="session", user_message="fan out")

    with pytest.raises(FanoutError, match="maximum is 2"):
        run_specialist_fanout(
            session_id="session",
            parent_run_id=parent.id,
            registry=[_spec()],
            branches=[
                {"agent_id": "worker", "task_kind": "analysis", "task": name, "project": name}
                for name in ("a", "b", "c")
            ],
            dispatch=lambda *args: {"status": "success", "summary": "done"},
            format_output=lambda spec, output: output["summary"],
            config=FanoutConfig(max_branches=2, max_concurrency=2),
        )


def test_fanout_honours_max_concurrency(monkeypatch):
    contexts = {name: _context(name) for name in ("a", "b", "c")}
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=contexts[value], error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    parent = get_task_run_store().create_run(session_id="session", user_message="fan out")
    lock = threading.Lock()
    current = 0
    maximum = 0

    def dispatch(spec, task, task_kind, project):
        nonlocal current, maximum
        with lock:
            current += 1
            maximum = max(maximum, current)
        time.sleep(0.05)
        with lock:
            current -= 1
        return {"status": "success", "summary": "done"}

    run_specialist_fanout(
        session_id="session",
        parent_run_id=parent.id,
        registry=[_spec()],
        branches=[
            {"agent_id": "worker", "task_kind": "analysis", "task": name, "project": name}
            for name in ("a", "b", "c")
        ],
        dispatch=dispatch,
        format_output=lambda spec, output: output["summary"],
        config=FanoutConfig(max_branches=4, max_concurrency=2),
    )

    assert maximum == 2


def test_fanout_surfaces_one_branch_failure_without_retry(monkeypatch):
    contexts = {"a": _context("a"), "b": _context("b")}
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=contexts[value], error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    parent = get_task_run_store().create_run(session_id="session", user_message="fan out")
    calls = []

    def dispatch(spec, task, task_kind, project):
        calls.append(project.project_id)
        if project.project_id == "project:b":
            raise RuntimeError("branch failed once")
        return {"status": "success", "summary": "done"}

    results = run_specialist_fanout(
        session_id="session",
        parent_run_id=parent.id,
        registry=[_spec()],
        branches=[
            {"agent_id": "worker", "task_kind": "analysis", "task": "one", "project": "a"},
            {"agent_id": "worker", "task_kind": "analysis", "task": "two", "project": "b"},
        ],
        dispatch=dispatch,
        format_output=lambda spec, output: output["summary"],
        config=FanoutConfig(max_branches=4, max_concurrency=2),
    )

    assert len(calls) == 2
    assert [item["state"] for item in results] == ["succeeded", "failed"]
    assert "branch failed once" in results[1]["response"]


def test_fanout_surfaces_branch_approval_pause(monkeypatch):
    contexts = {"a": _context("a"), "b": _context("b")}
    fake_registry = SimpleNamespace(
        resolve_known=lambda value: ProjectContextResolution(context=contexts[value], error=None)
    )
    monkeypatch.setattr(
        "agent_hub.specialist_fanout.get_project_context_registry",
        lambda: fake_registry,
    )
    store = get_task_run_store()
    parent = store.create_run(session_id="session", user_message="fan out")

    def dispatch(spec, task, task_kind, project):
        run_id = get_current_task_run_id()
        assert run_id is not None
        if project.project_id == "project:b":
            store.transition(
                run_id,
                TASK_STATE_WAITING_APPROVAL,
                detail="approval needed",
                selected_agent_id=spec.id,
                approval_token="approve-me",
            )
            return {"status": "approval_required", "summary": "approval needed"}
        return {"status": "success", "summary": "done"}

    results = run_specialist_fanout(
        session_id="session",
        parent_run_id=parent.id,
        registry=[_spec()],
        branches=[
            {"agent_id": "worker", "task_kind": "analysis", "task": "one", "project": "a"},
            {"agent_id": "worker", "task_kind": "analysis", "task": "two", "project": "b"},
        ],
        dispatch=dispatch,
        format_output=lambda spec, output: output["summary"],
        config=FanoutConfig(max_branches=4, max_concurrency=2),
    )

    assert [item["state"] for item in results] == ["succeeded", "waiting_approval"]
