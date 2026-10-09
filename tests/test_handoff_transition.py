"""Focused Phase 2 tests for the Hub-owned handoff transition gate."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage

import agent_hub.orchestrator as orchestrator_module
from agent_hub.handoff_transition import (
    FactoryHandoffEvidenceResolver,
    HandoffEvidenceError,
    HandoffFidelityReviewer,
    NextTaskContract,
    UnavailableHandoffEvidenceResolver,
)
from agent_hub.orchestrator import HubOrchestrator, RoutingDecision
from agent_hub.project_context import ProjectContext, ProjectContextResolution
from agent_hub.registry import AgentSpec
from agent_hub.task_control import TaskCancelled, get_task_control_registry
from agent_hub.task_runs import (
    TASK_STATE_CANCELLED,
    TASK_STATE_DISPATCHED,
    TASK_STATE_FAILED,
    TASK_STATE_IN_PROGRESS,
    TASK_STATE_ROUTED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_DECISION,
    active_task_run,
    get_task_run_store,
)

NEXT_TASK = {
    "task_kind": "coding_task",
    "task": "Implement the approved Shopping Agent package and behaviour.",
    "references": ["shopping://approved-design", "shopping://staged-package"],
}

PROJECT = ProjectContext(
    project_id="https://example.invalid/shopping.git",
    root="/tmp/shopping-agent",
    contract_version=1,
    fingerprint="shopping-fingerprint",
    metadata={"name": "shopping-agent"},
)

EVIDENCE = {
    "design_id": "shopping-design-v1",
    "package_id": "shopping-agent-package-v1",
    "task_kind": "coding_task",
    "task": NEXT_TASK["task"],
    "target_project": PROJECT.to_dict(),
    "references": list(NEXT_TASK["references"]),
    "purpose": "Implement the approved Shopping Agent package without changing its design.",
    "permissions": {"filesystem": "write", "network": False},
    "runtime": {"mode": "subprocess", "execution": "instruction_only"},
    "budgets": {"timeout_seconds": 600, "max_files": 20},
    "acceptance_criteria": ["Package tests pass", "No unapproved files change"],
    "stop_conditions": ["Stop on permission mismatch", "Stop on missing evidence"],
}

FACTORY_THREAD = "hub-factory-thread"
FACTORY_ROOT_CONTEXT = ProjectContext(
    project_id="https://example.invalid/agent-factory.git",
    root="/tmp/agent-factory",
    contract_version=1,
    fingerprint="factory-fingerprint",
    metadata={"name": "agent-factory"},
)


def _factory_resolution(next_task=None, *, thread_id=FACTORY_THREAD, correlation_id="corr-1"):
    next_task = next_task or {
        "task_kind": "coding_task",
        "task": "Implement the approved staged package.",
        "references": ["staging/agents/example-agent/BUILD_TASK.json", "docs/agent-contract.md"],
    }
    build_task = {
        "schema_version": 1,
        "agent_id": "example-agent",
        "agent_version": "1.0.0",
        "manifest_schema_version": 1,
        "manifest_sha256": "a" * 64,
        "staging_target": "staging/agents/example-agent",
        "permitted_paths": ["staging/agents/example-agent/src/**"],
        "acceptance_criteria": ["The package tests pass."],
        "test_commands": ["uv run pytest staging/agents/example-agent/tests -q"],
        "relevant_docs": ["docs/agent-contract.md"],
        "relevant_skills": ["skills/agent-authoring/SKILL.md"],
        "token_budget": 12000,
        "time_budget_seconds": 1800,
        "stop_conditions": ["Stop when validation cannot be satisfied."],
        "runtime_pattern": "deterministic_workflow",
        "runtime_pattern_reason": "The package workflow is fixed and inspectable.",
        "thread_id": thread_id,
        "correlation_id": correlation_id,
    }
    return {
        "status": "resolved",
        "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
        "thread_id": thread_id,
        "correlation_id": correlation_id,
        "build_task": build_task,
        "manifest": {
            "id": "example-agent",
            "version": "1.0.0",
            "manifest_schema_version": 1,
            "name": "Example Agent",
            "purpose": "Implement the approved example package.",
            "permissions": {"filesystem": "write", "network": False},
            "runtime": {"mode": "subprocess", "entrypoint": "run.sh"},
            "design": {
                "runtime_pattern": "deterministic_workflow",
                "runtime_pattern_reason": "The package workflow is fixed and inspectable.",
            },
        },
        "validated_references": [
            "staging/agents/example-agent/BUILD_TASK.json",
            "docs/agent-contract.md",
            "skills/agent-authoring/SKILL.md",
        ],
        "factory_result": {
            "status": "success",
            "summary": "Approved implementation handoff.",
            "next_task": next_task,
            "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
        },
    }


def _spec(
    agent_id: str = "shopping-implementer",
    *,
    task_kinds: tuple[str, ...] = ("coding_task",),
    runtime_mode: str = "subprocess",
) -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        name=agent_id.replace("-", " ").title(),
        purpose="Implements approved coding tasks.",
        runtime={"mode": runtime_mode},
        task_contract={"task_kinds": list(task_kinds)},
    )


class _UnusedGraph:
    pass


REVIEW_SUPPORTED = {
    "verdict": "supported",
    "coverage": {
        field: {"result": "supported", "detail": "Authoritative evidence matches."}
        for field in (
            "purpose",
            "permissions",
            "runtime_choice",
            "budgets_limits",
            "acceptance_criteria",
            "stop_conditions",
            "target_project",
            "scope",
        )
    },
    "omissions": [],
    "contradictions": [],
    "unexplained_scope_expansion": [],
    "unresolved_risks": [],
    "ambiguity": [],
}


class _FixtureEvidenceResolver:
    def __init__(self, evidence=None):
        self.evidence = evidence or EVIDENCE
        self.calls = []

    def resolve(
        self,
        references,
        next_task,
        project_context,
    ):
        self.calls.append((list(references), next_task, project_context))
        return self.evidence


class _FakeProjectRegistry:
    def __init__(self, resolution=None, live_context=None):
        self.resolution = resolution
        self.live_context = live_context
        self.calls = []

    def revalidate_context(self, context):
        self.calls.append(context)
        return self.resolution or ProjectContextResolution(context=context, error=None)

    def resolve_for_request(self, _session_id, _message):
        return self.resolution or ProjectContextResolution(context=PROJECT, error=None)

    def resolve_known(self, _reference):
        return self.resolution or ProjectContextResolution(context=PROJECT, error=None)

    def get(self, _session_id):
        return self.live_context


_DEFAULT_RESOLVER = object()


def _orchestrator(
    monkeypatch,
    reviewer,
    specs=None,
    resolver=_DEFAULT_RESOLVER,
    project_registry=None,
    routing_classifier=None,
    graph=None,
) -> HubOrchestrator:
    specs = specs or [_spec()]
    graph = graph or _UnusedGraph()
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self, *args, **kwargs: graph,
    )
    monkeypatch.setattr(orchestrator_module, "_load_specialists", lambda: specs)
    monkeypatch.setattr(
        orchestrator_module,
        "get_project_context_registry",
        lambda: project_registry or _FakeProjectRegistry(),
    )
    kwargs: dict[str, object] = {
        "model": "test",
        "routing_classifier": routing_classifier,
        "handoff_reviewer": reviewer,
    }
    if resolver is _DEFAULT_RESOLVER:
        kwargs["handoff_evidence_resolver"] = _FixtureEvidenceResolver()
    elif resolver is not None:
        kwargs["handoff_evidence_resolver"] = resolver
    return HubOrchestrator(**kwargs)


def _paused_origin(
    orchestrator: HubOrchestrator,
    *,
    depth: int = 0,
    selected_agent_id: str = "factory-brain",
    factory_thread_id: str | None = None,
):
    store = get_task_run_store()
    run = store.create_run(orchestrator.session_id, "Design the Shopping Agent")
    store.transition(run.id, TASK_STATE_ROUTED, selected_agent_id=selected_agent_id)
    store.transition(run.id, TASK_STATE_DISPATCHED, selected_agent_id=selected_agent_id)
    store.transition(run.id, TASK_STATE_IN_PROGRESS, selected_agent_id=selected_agent_id)
    store.update_run(
        run.id,
        context_updates={
            "originating_project_context": PROJECT.to_dict(),
            "cross_specialist_follow_on_depth": depth,
            **(
                {"agent_thread_id": factory_thread_id}
                if factory_thread_id is not None
                else {}
            ),
        },
    )
    return store.get_run(run.id)


class _NaturalLanguageFactoryGraph:
    """Deterministic Phase 2 fixture for the operator-level transition proof."""

    def invoke(self, _payload, *, config):
        run_id = orchestrator_module.get_current_task_run_id()
        assert run_id is not None
        store = get_task_run_store()
        store.transition(run_id, TASK_STATE_ROUTED, selected_agent_id="factory-brain")
        store.transition(run_id, TASK_STATE_DISPATCHED, selected_agent_id="factory-brain")
        store.transition(run_id, TASK_STATE_IN_PROGRESS, selected_agent_id="factory-brain")
        store.update_run(run_id, context_updates={"agent_thread_id": "fixture-factory-thread"})
        store.update_run(
            run_id,
            raw_result={
                "status": "success",
                "summary": "Deterministic Factory design fixture completed.",
                "next_task": NEXT_TASK,
            },
        )
        return {
            "messages": [
                AIMessage(
                    content=(
                        "I prepared an evidence-backed implementation handoff for your "
                        "surf-leash request."
                    )
                )
            ]
        }


def test_default_handoff_reviewer_uses_dedicated_runtime_model(monkeypatch):
    monkeypatch.setenv("HUB_MODEL", "hub-model")
    monkeypatch.setenv("HUB_HANDOFF_REVIEW_MODEL", "handoff-review-model")
    orch = _orchestrator(monkeypatch, reviewer=None)

    assert orch._handoff_reviewer.model == "handoff-review-model"


def _natural_language_orchestrator(monkeypatch):
    return _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=[
            _spec("factory-brain", task_kinds=("design_task",), runtime_mode="factory_brain"),
            _spec(),
        ],
        resolver=_FixtureEvidenceResolver(),
        project_registry=_FakeProjectRegistry(live_context=PROJECT),
        routing_classifier=lambda _message, _registry, *, model: RoutingDecision(
            route="specialist", task_kind="design_task", reason="deterministic fixture routing"
        ),
        graph=_NaturalLanguageFactoryGraph(),
    )


def test_natural_language_shopping_request_reaches_realistic_approval_surface(monkeypatch):
    orch = _natural_language_orchestrator(monkeypatch)
    reply = orch.invoke("Find me a 7-foot surf leash under $30 delivered to my house.")

    assert "Proposed implementation handoff" in reply
    assert "Implement the approved Shopping Agent package and behaviour." in reply
    pending = orch.pending_run()
    assert pending is not None
    assert pending.state == TASK_STATE_WAITING_DECISION
    assert pending.user_message == "Find me a 7-foot surf leash under $30 delivered to my house."

    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda *_args, **_kwargs: {
            "status": "success",
            "summary": "Fixture implementation complete.",
        },
    )
    assert orch.approve_pending() == "[Shopping Implementer] Fixture implementation complete."
    parent = get_task_run_store().get_run(pending.id)
    assert parent is not None
    child_id = parent.context["handoff_child_run_id"]
    child = get_task_run_store().get_run(child_id)
    assert child is not None
    assert child.context["handoff_parent_run_id"] == parent.id
    assert parent.id != child.id


def test_natural_language_request_changes_reenters_originating_factory_thread(monkeypatch):
    orch = _natural_language_orchestrator(monkeypatch)
    orch.invoke("Find me a 7-foot surf leash under $30 delivered to my house.")

    calls = []

    def revise(spec, task, **kwargs):
        calls.append((spec.id, task, kwargs, orchestrator_module.get_current_task_run_id()))
        return {
            "status": "success",
            "summary": "Factory revised the design.",
            "next_task": NEXT_TASK,
        }

    monkeypatch.setattr(orchestrator_module, "_dispatch_factory_brain", revise)
    reply = orch.provide_decision(
        "request_changes",
        "Please keep the leash under $30 and make delivery-to-home explicit.",
    )

    assert "Proposed implementation handoff" in reply
    assert calls == [
        (
            "factory-brain",
            "Please keep the leash under $30 and make delivery-to-home explicit.",
            {"thread_id": "fixture-factory-thread", "action": "invoke"},
            orch.pending_run().id,
        )
    ]
    pending = orch.pending_run()
    assert pending is not None
    assert pending.state == TASK_STATE_WAITING_DECISION
    assert pending.context["handoff_requested_correction"] == (
        "Please keep the leash under $30 and make delivery-to-home explicit."
    )
    assert pending.context["hub_transition_decision"]["decision"] == "waiting"


def test_revision_success_without_fresh_next_task_fails_closed(monkeypatch):
    orch = _natural_language_orchestrator(monkeypatch)
    orch.invoke("Find me a 7-foot surf leash under $30 delivered to my house.")
    pending = orch.pending_run()
    assert pending is not None

    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_factory_brain",
        lambda *_args, **_kwargs: {
            "status": "success",
            "summary": "Factory revised the design but omitted continuation.",
        },
    )
    reply = orch.provide_decision(
        "request_changes",
        "Please make delivery-to-home explicit.",
    )

    assert "fresh next_task" in reply
    failed = get_task_run_store().get_run(pending.id)
    assert failed is not None
    assert failed.state == TASK_STATE_FAILED
    assert failed.error_message is not None
    assert "fresh next_task" in failed.error_message


def test_valid_next_task_becomes_persisted_hub_transition(monkeypatch):
    seen = {}

    def review(payload):
        seen.update(payload)
        return REVIEW_SUPPORTED

    resolver = _FixtureEvidenceResolver()
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=review),
        resolver=resolver,
    )
    run = _paused_origin(orch)
    assert run is not None
    fabricated = {**EVIDENCE, "purpose": "Untrusted producer claim."}
    output = {
        "status": "success",
        "summary": "Design completed.",
        "next_task": NEXT_TASK,
        "approved_design_evidence": fabricated,
    }

    with active_task_run(run.id):
        packet = orch._prepare_handoff_transition(run, output)

    paused = get_task_run_store().get_run(run.id)
    assert paused is not None
    assert paused.state == TASK_STATE_WAITING_DECISION
    assert paused.context["hub_transition_decision"]["next_task"] == NEXT_TASK
    assert (
        paused.context["hub_transition_decision"]["approved_design_evidence"]["purpose"]
        == EVIDENCE["purpose"]
    )
    assert resolver.calls[0][0] == NEXT_TASK["references"]
    assert "APPROVE" in packet
    assert seen.keys() == {
        "approved_design_evidence",
        "proposed_next_task",
        "inherited_project_context",
        "eligible_specialists",
    }


def test_factory_resolver_maps_factory_validated_build_task_to_evidence(monkeypatch, tmp_path):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    monkeypatch.setattr(
        orchestrator_module,
        "get_project_context_registry",
        lambda: _FakeProjectRegistry(
            resolution=ProjectContextResolution(context=factory_project, error=None)
        ),
    )
    monkeypatch.setattr(
        "agent_hub.handoff_transition.get_project_context_registry",
        lambda: _FakeProjectRegistry(
            resolution=ProjectContextResolution(context=factory_project, error=None)
        ),
    )
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task",
        lambda **_kwargs: resolved,
    )

    resolver = FactoryHandoffEvidenceResolver(root)
    target = resolver.target_project_context(
        resolved["factory_result"]["next_task"]["references"],
        PROJECT,
        originating_agent_id="agent-factory",
    )
    evidence = resolver.resolve(
        resolved["factory_result"]["next_task"]["references"],
        NextTaskContract.model_validate(resolved["factory_result"]["next_task"]),
        target,
        originating_agent_id="agent-factory",
        factory_thread_id=FACTORY_THREAD,
    )

    assert target.root == str(root)
    assert evidence["design_id"] == "factory-build:corr-1"
    assert evidence["package_id"] == f"example-agent@1.0.0#{'a' * 64}"
    assert evidence["target_project"]["root"] == str(root)
    assert evidence["references"] == resolved["factory_result"]["next_task"]["references"]
    assert evidence["budgets"] == {"token_budget": 12000, "time_budget_seconds": 1800}
    assert evidence["other_constraints"]["correlation_id"] == "corr-1"
    assert evidence["other_constraints"]["artifact_reference"] == (
        "staging/agents/example-agent/BUILD_TASK.json"
    )
    assert evidence["other_constraints"]["permitted_paths"] == [
        "staging/agents/example-agent/src/**"
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "missing BUILD_TASK",
        "build task is not approved",
        "stale staged package",
        "tampered build task",
        "wrong Factory thread",
        "wrong Factory correlation",
        "traversal reference",
    ],
)
def test_factory_resolver_fails_closed_when_factory_bridge_rejects_reference(
    monkeypatch, tmp_path, failure
):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(failure)),
    )
    resolver = FactoryHandoffEvidenceResolver(root)
    next_task = NextTaskContract.model_validate(
        _factory_resolution()["factory_result"]["next_task"]
    )

    with pytest.raises(HandoffEvidenceError, match="could not be validated"):
        resolver.resolve(
            next_task.references or [],
            next_task,
            factory_project,
            originating_agent_id="agent-factory",
            factory_thread_id=FACTORY_THREAD,
        )


def test_factory_resolver_rejects_exact_next_task_mismatch(monkeypatch, tmp_path):
    root = tmp_path / "agent-factory"
    root.mkdir()
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task",
        lambda **_kwargs: resolved,
    )
    resolver = FactoryHandoffEvidenceResolver(root)
    mismatch = NextTaskContract.model_validate(
        {
            **resolved["factory_result"]["next_task"],
            "task": "Implement a different package.",
        }
    )

    with pytest.raises(HandoffEvidenceError, match="does not exactly match"):
        resolver.resolve(
            mismatch.references or [],
            mismatch,
            replace(FACTORY_ROOT_CONTEXT, root=str(root)),
            originating_agent_id="agent-factory",
            factory_thread_id=FACTORY_THREAD,
        )


def test_factory_handoff_approves_child_with_factory_root_context(monkeypatch, tmp_path):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    registry = _FakeProjectRegistry(
        resolution=ProjectContextResolution(context=factory_project, error=None)
    )
    monkeypatch.setattr(
        "agent_hub.handoff_transition.get_project_context_registry", lambda: registry
    )
    monkeypatch.setattr("agent_hub.handoff_transition.AGENT_FACTORY_ROOT", root)
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task",
        lambda **_kwargs: resolved,
    )
    specs = [
        _spec("agent-factory", task_kinds=("design_task",), runtime_mode="factory_brain"),
        _spec("ai-tech-lead"),
    ]
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=specs,
        project_registry=registry,
        resolver=None,
    )
    run = _paused_origin(
        orch,
        selected_agent_id="agent-factory",
        factory_thread_id=FACTORY_THREAD,
    )
    next_task = NextTaskContract.model_validate(resolved["factory_result"]["next_task"])
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": next_task.model_dump(mode="json")},
        )

    pending = get_task_run_store().get_run(run.id)
    assert pending is not None
    handoff = pending.context["hub_transition_decision"]
    assert handoff["project_context"]["root"] == str(root)
    assert handoff["originating_project_context"]["root"] == PROJECT.root

    calls = []
    receipt = {
        "status": "validated",
        "thread_id": FACTORY_THREAD,
        "correlation_id": "corr-1",
        "agent_id": "example-agent",
        "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
        "build_result_reference": "staging/agents/example-agent/BUILD_RESULT.json",
    }
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda spec, task, **kwargs: calls.append((spec.id, task, kwargs))
        or {
            "status": "success",
            "summary": "Implemented.",
            "build_result": {"status": "success", "tests_run": []},
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "relay_factory_build_result",
        lambda **_kwargs: receipt,
    )
    assert "Implemented." in orch.approve_pending()
    assert calls[0][0] == "ai-tech-lead"
    assert calls[0][2]["human_approved"] is True
    assert calls[0][2]["project_root_override"] == str(root)
    assert calls[0][2]["project_context_override"].root == str(root)
    assert calls[0][2]["task_kind"] == "coding_task"
    assert calls[0][2]["references"] == next_task.references
    assert calls[0][2]["execution_constraints"] == {
        "schema_version": 1,
        "token_budget": 12000,
        "time_budget_seconds": 1800,
        "test_commands": ["uv run pytest staging/agents/example-agent/tests -q"],
        "permitted_paths": ["staging/agents/example-agent/src/**"],
        "stop_conditions": ["Stop when validation cannot be satisfied."],
        "correlation_id": "corr-1",
        "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
    }
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None and parent.state == TASK_STATE_SUCCEEDED
    child = get_task_run_store().get_run(parent.context["handoff_child_run_id"])
    assert child is not None and child.state == TASK_STATE_SUCCEEDED
    assert child.context["execution_constraints"] == calls[0][2]["execution_constraints"]
    assert parent.context["factory_validation_receipt"] == receipt
    assert child.context["factory_build_result_reference"] == receipt["artifact_reference"]
    assert parent.raw_result["factory_validation_receipt"] == receipt


def test_generic_handoff_does_not_invent_factory_constraints(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
    )
    run = _paused_origin(orch)
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    calls = []
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda spec, task, **kwargs: calls.append(kwargs)
        or {"status": "success", "summary": "Implemented."},
    )
    assert "Implemented." in orch.approve_pending()
    assert "execution_constraints" not in calls[0]


def test_factory_decision_resume_reuses_checkpoint_constraints_and_approval(monkeypatch):
    spec = _spec("ai-tech-lead")
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=[spec],
    )
    store = get_task_run_store()
    run = store.create_run(orch.session_id, "Implement the approved staged package")
    store.transition(run.id, TASK_STATE_ROUTED, selected_agent_id=spec.id)
    store.transition(run.id, TASK_STATE_DISPATCHED, selected_agent_id=spec.id)
    store.transition(run.id, TASK_STATE_IN_PROGRESS, selected_agent_id=spec.id)
    store.transition(
        run.id,
        TASK_STATE_WAITING_DECISION,
        selected_agent_id=spec.id,
        context_updates={
            "agent_request_id": "atl-request-1",
            "agent_dispatch_task_kind": "coding_task",
            "specialist_pending_decision": {"options": [{"name": "answer"}]},
            "execution_constraints": {
                "schema_version": 1,
                "token_budget": 250000,
                "time_budget_seconds": 900,
                "correlation_id": "corr-1",
                "artifact_reference": "staging/agents/example-agent/BUILD_TASK.json",
            },
        },
    )

    calls = []
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda _spec, _task, **kwargs: calls.append(kwargs)
        or {"status": "success", "summary": "Resumed."},
    )

    assert orch.provide_decision("answer") == "[Ai Tech Lead] Resumed."
    assert calls == [
        {
            "request_id": "atl-request-1",
            "decision": {"option": "answer", "text": "", "actor": "human"},
            "project_root_override": None,
            "project_context_override": None,
            "references": None,
            "backlog_reference_override": None,
            "task_kind": "coding_task",
            "result_registry": orch._registry,
            "human_approved": True,
        }
    ]


@pytest.mark.parametrize("build_status", ["failed", "stopped"])
def test_factory_build_failure_is_authoritative_even_when_atl_reports_success(
    monkeypatch, tmp_path, build_status
):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    registry = _FakeProjectRegistry(
        resolution=ProjectContextResolution(context=factory_project, error=None)
    )
    monkeypatch.setattr(
        "agent_hub.handoff_transition.get_project_context_registry", lambda: registry
    )
    monkeypatch.setattr("agent_hub.handoff_transition.AGENT_FACTORY_ROOT", root)
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task", lambda **_kwargs: resolved
    )
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=[
            _spec("agent-factory", task_kinds=("design_task",), runtime_mode="factory_brain"),
            _spec("ai-tech-lead"),
        ],
        project_registry=registry,
        resolver=None,
    )
    run = _paused_origin(orch, selected_agent_id="agent-factory", factory_thread_id=FACTORY_THREAD)
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": resolved["factory_result"]["next_task"]},
        )
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda *_args, **_kwargs: {
            "status": "success",
            "summary": "ATL top-level success.",
            "build_result": {"status": build_status, "errors": ["validation did not pass"]},
        },
    )
    monkeypatch.setattr(
        orchestrator_module,
        "relay_factory_build_result",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Factory rejected BuildResult")),
    )

    reply = orch.approve_pending()
    assert "failed closed" in reply
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None and parent.state == TASK_STATE_FAILED
    child = get_task_run_store().get_run(parent.context["handoff_child_run_id"])
    assert child is not None and child.state == TASK_STATE_FAILED
    assert child.raw_result["factory_build_result"]["status"] == build_status


def test_factory_child_without_build_result_fails_closed(monkeypatch, tmp_path):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    registry = _FakeProjectRegistry(
        resolution=ProjectContextResolution(context=factory_project, error=None)
    )
    monkeypatch.setattr(
        "agent_hub.handoff_transition.get_project_context_registry", lambda: registry
    )
    monkeypatch.setattr("agent_hub.handoff_transition.AGENT_FACTORY_ROOT", root)
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task", lambda **_kwargs: resolved
    )
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=[
            _spec("agent-factory", task_kinds=("design_task",), runtime_mode="factory_brain"),
            _spec("ai-tech-lead"),
        ],
        project_registry=registry,
        resolver=None,
    )
    run = _paused_origin(orch, selected_agent_id="agent-factory", factory_thread_id=FACTORY_THREAD)
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": resolved["factory_result"]["next_task"]},
        )
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda *_args, **_kwargs: {"status": "success", "summary": "No evidence."},
    )
    relay_called = False

    def relay(**_kwargs):
        nonlocal relay_called
        relay_called = True
        raise AssertionError("missing build_result must fail before Factory relay")

    monkeypatch.setattr(orchestrator_module, "relay_factory_build_result", relay)
    reply = orch.approve_pending()
    assert "no build_result" in reply
    assert relay_called is False
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None and parent.state == TASK_STATE_FAILED


def test_factory_waiting_decision_is_not_relayed(monkeypatch, tmp_path):
    root = tmp_path / "agent-factory"
    root.mkdir()
    factory_project = replace(FACTORY_ROOT_CONTEXT, root=str(root))
    registry = _FakeProjectRegistry(
        resolution=ProjectContextResolution(context=factory_project, error=None)
    )
    monkeypatch.setattr(
        "agent_hub.handoff_transition.get_project_context_registry", lambda: registry
    )
    monkeypatch.setattr("agent_hub.handoff_transition.AGENT_FACTORY_ROOT", root)
    resolved = _factory_resolution()
    monkeypatch.setattr(
        "agent_hub.factory_bridge.resolve_factory_build_task", lambda **_kwargs: resolved
    )
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs=[
            _spec("agent-factory", task_kinds=("design_task",), runtime_mode="factory_brain"),
            _spec("ai-tech-lead"),
        ],
        project_registry=registry,
        resolver=None,
    )
    run = _paused_origin(orch, selected_agent_id="agent-factory", factory_thread_id=FACTORY_THREAD)
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": resolved["factory_result"]["next_task"]},
        )
    def paused_dispatch(spec, *_args, **_kwargs):
        output = {
            "status": "success",
            "summary": "ATL needs a decision.",
            "pending_decision": {
                "prompt": "Choose a package option.",
                "options": [{"name": "approve"}],
            },
        }
        orchestrator_module._record_agent_status(
            spec,
            output,
            orchestrator_module.get_current_task_run_id(),
        )
        return output

    monkeypatch.setattr(orchestrator_module, "_dispatch_subprocess", paused_dispatch)
    monkeypatch.setattr(
        orchestrator_module,
        "relay_factory_build_result",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pause was relayed")),
    )
    reply = orch.approve_pending()
    assert "ATL needs a decision." in reply
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None and parent.state == TASK_STATE_IN_PROGRESS
    child = get_task_run_store().get_run(parent.context["handoff_child_run_id"])
    assert child is not None and child.state == TASK_STATE_WAITING_DECISION


def test_default_handoff_resolver_fails_closed_for_non_factory_source(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        resolver=None,
    )
    run = _paused_origin(orch)

    with pytest.raises(HandoffEvidenceError, match="authoritative Factory design evidence"):
        orch._prepare_handoff_transition(run, {"status": "success", "next_task": NEXT_TASK})

    assert get_task_run_store().get_run(run.id).state == TASK_STATE_IN_PROGRESS


def test_deterministic_validation_precedes_reviewer_and_missing_evidence_fails_closed(monkeypatch):
    called = False

    def review(_payload):
        nonlocal called
        called = True
        raise AssertionError("reviewer must not run")

    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=review),
        resolver=UnavailableHandoffEvidenceResolver(),
    )
    run = _paused_origin(orch)
    assert run is not None
    with pytest.raises(ValueError, match="authoritative Factory design evidence"):
        orch._prepare_handoff_transition(
            run,
            {
                "status": "success",
                "next_task": NEXT_TASK,
                "approved_design_evidence": EVIDENCE,
            },
        )
    assert called is False
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_IN_PROGRESS


def test_unresolved_authoritative_reference_fails_before_reviewer(monkeypatch):
    called = False

    def review(_payload):
        nonlocal called
        called = True
        return REVIEW_SUPPORTED

    evidence = {**EVIDENCE, "references": [NEXT_TASK["references"][0]]}
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=review),
        resolver=_FixtureEvidenceResolver(evidence),
    )
    run = _paused_origin(orch)
    assert run is not None
    with pytest.raises(ValueError, match="not present in authoritative design evidence"):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK},
        )
    assert called is False


def test_reviewer_requires_complete_supporting_coverage(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(
            review_callable=lambda _payload: {
                **REVIEW_SUPPORTED,
                "coverage": {
                    **REVIEW_SUPPORTED["coverage"],
                    "scope": {"result": "needs_attention", "detail": "Scope is unclear."},
                },
            }
        ),
    )
    run = _paused_origin(orch)
    assert run is not None
    with pytest.raises(ValueError, match="supported verdict"):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK},
        )
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_IN_PROGRESS


def test_reviewer_failure_invalidates_checkpoint(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: {"verdict": "invalid"}),
    )
    run = _paused_origin(orch)
    assert run is not None
    with pytest.raises(ValueError):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_IN_PROGRESS


def test_multiple_eligible_specialists_require_explicit_human_choice(monkeypatch):
    specs = [_spec("first"), _spec("second")]
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(
            review_callable=lambda _payload: {
                "verdict": "needs_attention",
                "coverage": {
                    field: {"result": "supported", "detail": "Evidence present."}
                    for field in REVIEW_SUPPORTED["coverage"]
                },
                "omissions": [],
                "contradictions": [],
                "unexplained_scope_expansion": [],
                "unresolved_risks": [],
                "ambiguity": ["Two eligible specialists remain."],
            }
        ),
        specs,
    )
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        packet = orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    assert "human choice required" in packet
    assert orch.approve_pending() == (
        "Multiple specialists are eligible. Reply with APPROVE followed by one exact "
        "eligible specialist id."
    )


def test_approve_dispatches_frozen_task_once_and_request_changes_reject_do_not(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(
            review_callable=lambda _payload: REVIEW_SUPPORTED
        ),
    )
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )

    calls = []

    def dispatch(spec, task, **kwargs):
        calls.append((spec.id, task, kwargs, orchestrator_module.get_current_task_run_id()))
        return {"status": "success", "summary": "Implemented."}

    monkeypatch.setattr(orchestrator_module, "_dispatch_subprocess", dispatch)
    assert orch.approve_pending() == "[Shopping Implementer] Implemented."
    assert calls[0][:3] == (
        "shopping-implementer",
        NEXT_TASK["task"],
        {
            "references": NEXT_TASK["references"],
            "project_root_override": PROJECT.root,
            "project_context_override": PROJECT,
            "backlog_reference_override": None,
            "task_kind": "coding_task",
            "result_registry": orch.registry,
        },
    )
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None
    assert parent.state == TASK_STATE_SUCCEEDED
    child_id = parent.context["handoff_child_run_id"]
    assert calls[0][3] == child_id
    child = get_task_run_store().get_run(child_id)
    assert child is not None
    assert child.id != parent.id
    assert child.state == TASK_STATE_SUCCEEDED
    assert child.context["handoff_parent_run_id"] == parent.id

    third = _paused_origin(orch)
    assert third is not None
    with active_task_run(third.id):
        orch._prepare_handoff_transition(
            third,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    assert "rejected" in orch.provide_decision("reject", "Not approved").lower()
    assert get_task_run_store().get_run(third.id).state == TASK_STATE_CANCELLED
    assert len(calls) == 1


def test_stopping_handoff_parent_cancels_active_sequential_child(monkeypatch):
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
    )
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )

    child_started = threading.Event()

    def dispatch(*_args, **_kwargs):
        child_id = orchestrator_module.get_current_task_run_id()
        assert child_id is not None
        child_started.set()
        while True:
            handle = get_task_control_registry().get_handle(child_id)
            if handle is not None and handle.cancel_requested:
                raise TaskCancelled(handle.cancellation_reason or "Stopped by user")
            time.sleep(0.01)

    monkeypatch.setattr(orchestrator_module, "_dispatch_subprocess", dispatch)
    result = {}

    def approve():
        try:
            result["reply"] = orch.approve_pending()
        except TaskCancelled:
            result["cancelled"] = True

    worker = threading.Thread(target=approve)
    worker.start()
    assert child_started.wait(timeout=2)

    confirmation = orch.stop_current_task(identifier=run.id)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert "sequential handoff child" in confirmation
    parent = get_task_run_store().get_run(run.id)
    assert parent is not None
    assert parent.state == TASK_STATE_SUCCEEDED
    child_id = parent.context["handoff_child_run_id"]
    child = get_task_run_store().get_run(child_id)
    assert child is not None
    assert child.state == TASK_STATE_CANCELLED
    assert parent.context["handoff_child_status"] == "cancelled"
    assert result["cancelled"] is True


@pytest.mark.parametrize(
    "mutation",
    ["removed", "task_kind_removed", "runtime_changed", "contract_changed"],
)
def test_approval_revalidates_live_specialist_before_dispatch(monkeypatch, mutation):
    specs = [_spec()]
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        specs,
    )
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK},
        )

    if mutation == "removed":
        specs.clear()
    elif mutation == "task_kind_removed":
        specs[0] = replace(specs[0], task_contract={"task_kinds": []})
    elif mutation == "runtime_changed":
        specs[0] = replace(specs[0], runtime={"mode": "subprocess", "changed": True})
    else:
        specs[0] = replace(
            specs[0],
            task_contract={"task_kinds": ["coding_task"], "contract_revision": 2},
        )

    calls = []
    monkeypatch.setattr(
        orchestrator_module,
        "_dispatch_subprocess",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    result = orch.approve_pending()
    assert "handoff" in result.lower() or "changed" in result.lower()
    assert calls == []
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_FAILED


def test_approval_revalidates_frozen_project_without_using_current_selection(monkeypatch):
    project_registry = _FakeProjectRegistry(
        ProjectContextResolution(context=None, error="frozen project root disappeared")
    )
    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED),
        project_registry=project_registry,
    )
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK},
        )

    result = orch.approve_pending()
    assert "frozen project root disappeared" in result
    assert project_registry.calls == [PROJECT]
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_FAILED


def test_material_change_depth_and_stop_prevent_approval(monkeypatch):
    reviewer = HandoffFidelityReviewer(review_callable=lambda _payload: REVIEW_SUPPORTED)
    orch = _orchestrator(monkeypatch, reviewer)
    run = _paused_origin(orch)
    assert run is not None
    with active_task_run(run.id):
        orch._prepare_handoff_transition(
            run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    mutated = dict(get_task_run_store().get_run(run.id).context["hub_transition_decision"])
    mutated["next_task"] = {**NEXT_TASK, "task": "Changed after review."}
    get_task_run_store().update_context(run.id, hub_transition_decision=mutated)
    assert "changed after review" in orch.approve_pending().lower()
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_FAILED

    depth_run = _paused_origin(orch, depth=1)
    assert depth_run is not None
    with pytest.raises(ValueError, match="depth"):
        orch._prepare_handoff_transition(
            depth_run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )

    stop_run = _paused_origin(orch)
    assert stop_run is not None
    with active_task_run(stop_run.id):
        orch._prepare_handoff_transition(
            stop_run,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    assert "cancelled" in orch.stop_current_task().lower()
    assert orch.approve_pending() == "No task is currently waiting for approval."
