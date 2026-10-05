"""Evidence-backed cross-specialist handoff transition support.

This module deliberately has no registry, subprocess, filesystem, or tool
access.  It validates the bounded evidence packet and provides the separate
read-only reviewer context used by Hub before presenting a human checkpoint.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator

from .config import (
    chat_model_kwargs,
    configured_handoff_reviewer_max_tokens,
    configured_handoff_reviewer_model,
    configured_handoff_reviewer_timeout_seconds,
)
from .cost_log import extract_usage_metadata, record_llm_run
from .project_context import ProjectContext
from .specialist_result import NextTaskContract

logger = logging.getLogger(__name__)
MAX_APPROVED_EVIDENCE_BYTES = 64 * 1024


class HandoffEvidenceError(ValueError):
    """Authoritative evidence is missing, malformed, or does not resolve."""


class TargetProjectEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: StrictStr
    root: StrictStr
    contract_version: int
    fingerprint: StrictStr
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovedDesignEvidence(BaseModel):
    """The structured Factory evidence required for a Phase 2 checkpoint."""

    model_config = ConfigDict(extra="forbid")

    design_id: StrictStr
    package_id: StrictStr
    task_kind: StrictStr
    task: StrictStr
    target_project: TargetProjectEvidence | None = None
    references: list[StrictStr]
    purpose: StrictStr
    permissions: dict[str, Any]
    runtime: dict[str, Any]
    budgets: dict[str, Any]
    acceptance_criteria: list[StrictStr]
    stop_conditions: list[StrictStr]
    other_constraints: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "design_id",
        "package_id",
        "task_kind",
        "task",
        "purpose",
    )
    @classmethod
    def require_non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("references", "acceptance_criteria", "stop_conditions")
    @classmethod
    def require_bounded_non_empty_strings(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must contain at least one item")
        if any(not item.strip() for item in value):
            raise ValueError("items must be non-empty strings")
        return value


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: StrictStr
    result: Literal["supported", "needs_attention", "mismatch"]
    detail: StrictStr


class HandoffFidelityReview(BaseModel):
    """Strict, advisory reviewer output.  It never authorizes dispatch."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["supported", "needs_attention", "mismatch"]
    findings: list[ReviewFinding]
    omissions: list[StrictStr]
    contradictions: list[StrictStr]
    unexplained_scope_expansion: list[StrictStr]
    unresolved_risks: list[StrictStr]
    ambiguity: list[StrictStr]


def resolve_approved_design_evidence(
    raw_evidence: Any,
    next_task: NextTaskContract,
    project_context: ProjectContext | None,
) -> ApprovedDesignEvidence:
    """Resolve only a structured evidence object explicitly carried by Hub.

    The producer may reference evidence with the exact ``next_task.references``
    values, but Hub never reconstructs constraints from a prose response.
    """
    if not isinstance(raw_evidence, Mapping):
        raise HandoffEvidenceError(
            "required approved_design_evidence is missing or is not a structured object"
        )
    try:
        encoded_evidence = json.dumps(raw_evidence, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise HandoffEvidenceError("approved design evidence is not JSON-serializable") from exc
    if len(encoded_evidence.encode("utf-8")) > MAX_APPROVED_EVIDENCE_BYTES:
        raise HandoffEvidenceError(
            "approved design evidence exceeds the bounded 64 KiB reviewer input limit"
        )
    try:
        evidence = ApprovedDesignEvidence.model_validate(raw_evidence)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise HandoffEvidenceError(f"invalid approved design evidence: {details}") from exc

    unresolved = [
        reference
        for reference in (next_task.references or [])
        if reference not in evidence.references
    ]
    if unresolved:
        raise HandoffEvidenceError(
            "required handoff reference(s) are not present in authoritative design evidence: "
            + ", ".join(unresolved)
        )
    if evidence.target_project is None:
        if project_context is not None:
            raise HandoffEvidenceError(
                "authoritative design evidence has no target project, but the originating run "
                "has a validated project context"
            )
    elif project_context is None:
        raise HandoffEvidenceError(
            "authoritative design evidence names a target project, but the originating run "
            "has no validated project context"
        )
    else:
        target = evidence.target_project
        actual = project_context
        if (
            target.project_id != actual.project_id
            or target.root != actual.root
            or target.contract_version != actual.contract_version
            or target.fingerprint != actual.fingerprint
        ):
            raise HandoffEvidenceError(
                "authoritative target project does not match the inherited validated "
                "project context"
            )
    if evidence.task_kind != next_task.task_kind:
        raise HandoffEvidenceError(
            "authoritative design evidence task_kind does not match next_task.task_kind"
        )
    if evidence.task != next_task.task:
        raise HandoffEvidenceError(
            "authoritative design evidence task does not match next_task.task"
        )
    return evidence


def _review_payload(
    evidence: ApprovedDesignEvidence,
    next_task: NextTaskContract,
    project_context: ProjectContext | None,
    eligible_specialists: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the complete and bounded reviewer input; no conversation is included."""
    return {
        "approved_design_evidence": evidence.model_dump(mode="json"),
        "proposed_next_task": next_task.model_dump(mode="json"),
        "inherited_project_context": (
            project_context.to_dict() if project_context is not None else None
        ),
        "eligible_specialists": [dict(item) for item in eligible_specialists],
    }


class HandoffFidelityReviewer:
    """Governed Hub-owned reviewer with a separate, tool-free model call."""

    def __init__(
        self,
        *,
        model: str | None = None,
        review_callable: Callable[[dict[str, Any]], Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.model = model or configured_handoff_reviewer_model()
        self.review_callable = review_callable
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else configured_handoff_reviewer_timeout_seconds()
        )

    def review(
        self,
        evidence: ApprovedDesignEvidence,
        next_task: NextTaskContract,
        project_context: ProjectContext | None,
        eligible_specialists: Sequence[Mapping[str, Any]],
    ) -> HandoffFidelityReview:
        payload = _review_payload(evidence, next_task, project_context, eligible_specialists)
        if self.review_callable is not None:
            result = self.review_callable(payload)
            return HandoffFidelityReview.model_validate(result)

        prompt = (
            "Review whether this exact proposed implementation handoff faithfully represents "
            "the approved structured Factory design. Return only the requested structured "
            "review. The verdict is advisory evidence for a human and is not execution "
            "authority. Do not invent missing constraints.\n\n"
            + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        usage_cb = UsageMetadataCallbackHandler()
        started_at = time.perf_counter()
        status = "ok"
        error: str | None = None
        result: Any = None
        try:
            kwargs = chat_model_kwargs(self.model)
            kwargs.update(
                {
                    "timeout": self.timeout_seconds,
                    "max_retries": 0,
                    "max_tokens": configured_handoff_reviewer_max_tokens(),
                }
            )
            llm = ChatOpenAI(**kwargs).with_structured_output(HandoffFidelityReview)
            result = llm.invoke([HumanMessage(content=prompt)], config={"callbacks": [usage_cb]})
            return HandoffFidelityReview.model_validate(result)
        except Exception as exc:
            status = "error"
            error = str(exc)
            raise
        finally:
            try:
                record_llm_run(
                    operation="hub_handoff_fidelity_review",
                    request_kind="handoff-review",
                    requested_model=self.model,
                    effective_model=self.model,
                    status=status,
                    duration_seconds=time.perf_counter() - started_at,
                    usage_by_model=extract_usage_metadata(usage_cb),
                    error=error,
                    result_preview=(
                        _bounded_preview(result) if status == "ok" else None
                    ),
                )
            except Exception:
                logger.exception("Could not record handoff reviewer usage")


def _bounded_preview(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value.model_dump(mode="json"), sort_keys=True)
    except AttributeError:
        text = json.dumps(value, sort_keys=True, default=str)
    return text[:500]
