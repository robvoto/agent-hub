"""Focused Phase 2 tests for the Hub-owned handoff transition gate."""

from __future__ import annotations

from dataclasses import replace

import pytest

import agent_hub.orchestrator as orchestrator_module
from agent_hub.handoff_transition import HandoffFidelityReviewer
from agent_hub.orchestrator import HubOrchestrator
from agent_hub.project_context import ProjectContext, ProjectContextResolution
from agent_hub.registry import AgentSpec
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


def _spec(agent_id: str = "shopping-implementer") -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        name=agent_id.replace("-", " ").title(),
        purpose="Implements approved coding tasks.",
        runtime={"mode": "subprocess"},
        task_contract={"task_kinds": ["coding_task"]},
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

    def resolve(self, references, next_task, project_context):
        self.calls.append((list(references), next_task, project_context))
        return self.evidence


class _FakeProjectRegistry:
    def __init__(self, resolution=None):
        self.resolution = resolution
        self.calls = []

    def revalidate_context(self, context):
        self.calls.append(context)
        return self.resolution or ProjectContextResolution(context=context, error=None)

    def get(self, _session_id):
        return None


def _orchestrator(
    monkeypatch,
    reviewer,
    specs=None,
    resolver=None,
    project_registry=None,
) -> HubOrchestrator:
    specs = specs or [_spec()]
    monkeypatch.setattr(
        HubOrchestrator,
        "_build_graph",
        lambda self, *args, **kwargs: _UnusedGraph(),
    )
    monkeypatch.setattr(orchestrator_module, "_load_specialists", lambda: specs)
    monkeypatch.setattr(
        orchestrator_module,
        "get_project_context_registry",
        lambda: project_registry or _FakeProjectRegistry(),
    )
    return HubOrchestrator(
        model="test",
        handoff_reviewer=reviewer,
        handoff_evidence_resolver=resolver or _FixtureEvidenceResolver(),
    )


def _paused_origin(orchestrator: HubOrchestrator, *, depth: int = 0):
    store = get_task_run_store()
    run = store.create_run(orchestrator.session_id, "Design the Shopping Agent")
    store.transition(run.id, TASK_STATE_ROUTED, selected_agent_id="factory-brain")
    store.transition(run.id, TASK_STATE_DISPATCHED, selected_agent_id="factory-brain")
    store.transition(run.id, TASK_STATE_IN_PROGRESS, selected_agent_id="factory-brain")
    store.update_run(
        run.id,
        context_updates={
            "originating_project_context": PROJECT.to_dict(),
            "cross_specialist_follow_on_depth": depth,
        },
    )
    return store.get_run(run.id)


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


def test_deterministic_validation_precedes_reviewer_and_missing_evidence_fails_closed(monkeypatch):
    called = False

    def review(_payload):
        nonlocal called
        called = True
        raise AssertionError("reviewer must not run")

    orch = _orchestrator(
        monkeypatch,
        HandoffFidelityReviewer(review_callable=review),
        resolver=orchestrator_module.UnavailableHandoffEvidenceResolver(),
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
        calls.append((spec.id, task, kwargs))
        return {"status": "success", "summary": "Implemented."}

    monkeypatch.setattr(orchestrator_module, "_dispatch_subprocess", dispatch)
    assert orch.approve_pending() == "[Shopping Implementer] Implemented."
    assert calls == [
        (
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
    ]
    assert get_task_run_store().get_run(run.id).state == TASK_STATE_SUCCEEDED

    second = _paused_origin(orch)
    assert second is not None
    with active_task_run(second.id):
        orch._prepare_handoff_transition(
            second,
            {"status": "success", "next_task": NEXT_TASK, "approved_design_evidence": EVIDENCE},
        )
    assert "Human requested changes" in orch.provide_decision("request_changes", "Fix the scope")
    assert get_task_run_store().get_run(second.id).state == TASK_STATE_SUCCEEDED

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
