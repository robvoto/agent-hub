"""Deterministic validation for structured specialist result extensions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, ValidationError, field_validator

from .registry import AgentSpec


class NextTaskContract(BaseModel):
    """The Phase 1 specialist result contract for describing follow-on work."""

    model_config = ConfigDict(extra="forbid")

    task_kind: StrictStr
    task: StrictStr
    references: list[StrictStr] | None = None

    @field_validator("task_kind", "task")
    @classmethod
    def require_non_empty_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("references", mode="before")
    @classmethod
    def require_list_when_present(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("must be a list of strings when present")
        return value


class SpecialistResultContractError(ValueError):
    """A specialist result violates the explicit Hub result contract."""


def validate_specialist_result(
    output: Any,
    registry: list[AgentSpec],
) -> None:
    """Validate an explicit ``next_task`` against schema and live registry.

    A missing ``next_task`` is unchanged. A present value must be an exact
    object-shaped contract, and its task kind must be advertised by at least
    one specialist in the current enabled registry.
    """
    if not isinstance(output, dict):
        raise SpecialistResultContractError("specialist result must be a JSON object")
    if "next_task" not in output:
        return
    if output.get("status") != "success":
        raise SpecialistResultContractError(
            "next_task is only allowed when specialist result status is 'success'"
        )

    value = output["next_task"]
    if not isinstance(value, dict):
        raise SpecialistResultContractError("next_task must be a JSON object")
    try:
        next_task = NextTaskContract.model_validate(value)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise SpecialistResultContractError(f"invalid next_task contract: {details}") from exc

    if not any(
        isinstance(spec.task_contract, dict)
        and isinstance(spec.task_contract.get("task_kinds"), list)
        and next_task.task_kind in spec.task_contract["task_kinds"]
        for spec in registry
    ):
        raise SpecialistResultContractError(
            "next_task.task_kind is not advertised by an eligible specialist in the current "
            f"registry: {next_task.task_kind!r}"
        )
