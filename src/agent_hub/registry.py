"""Agent registry — reads enabled agents from the agent-factory config directory.

Enabled agents live in agent-factory/config/agents/<id>/agent.json.
The registry loads all of them at startup so the orchestrator can route to them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AGENT_REGISTRY_DIR

logger = logging.getLogger(__name__)


_CORE_FIELDS = {
    "id",
    "name",
    "purpose",
    "tools",
    "version",
    "runtime",
    "input_contract",
    "interaction_contract",
    "hub_integration",
}


@dataclass(frozen=True)
class AgentSpec:
    """Minimal view of an enabled agent needed for routing and dispatch.

    `input_contract`/`interaction_contract` are the specialist's declared
    universal-envelope and lifecycle-capability metadata (see Agent Factory's
    `docs/agent-contract.md`). Most of it is discovery data Hub stores but
    does not act on; the exception is `input_contract.accepted_context` /
    `required_context`, which Hub reads generically to decide what dispatch
    context (`project_root`, `references`) a specialist actually gets and
    whether a dispatch has what it needs (see `_resolve_dispatch_context` in
    orchestrator.py).

    Any agent.json field outside the core set above (e.g. a specialist's own
    backlog pointer, a knowledge_db path, or a future custom field) lands in
    `extensions` unchanged — Hub never needs a new named field, let alone new
    dispatch logic, for a specialist to declare specialist-owned metadata.
    """

    id: str
    name: str
    purpose: str
    tools: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    input_contract: dict[str, Any] = field(default_factory=dict)
    interaction_contract: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    hub_integration: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegistryLoadError:
    """One agent.json Hub could not load, so it can be surfaced instead of
    silently vanishing from the callable specialist set (see AGENT-HUB-040)."""

    source: str
    message: str


@dataclass(frozen=True)
class RegistryLoadResult:
    specs: list[AgentSpec]
    errors: list[RegistryLoadError]


def spec_fingerprint(spec: AgentSpec) -> str:
    """Stable content hash of an agent spec.

    Used to pin an in-flight (paused) task to the exact manifest version it
    was dispatched against, and to detect whether the live registry's entry
    for an agent id still matches what a paused task was pinned to (see
    `HubOrchestrator._require_spec` in orchestrator.py).
    """
    payload = json.dumps(dataclasses.asdict(spec), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_agent_spec(data: dict[str, Any]) -> AgentSpec:
    """Parse one agent.json object into the Hub's bounded runtime view."""
    if not isinstance(data, dict):
        raise ValueError("agent.json must contain a JSON object.")

    return AgentSpec(
        id=data["id"],
        name=data.get("name", data["id"]),
        purpose=data.get("purpose", ""),
        tools=list(data.get("tools", [])),
        version=data.get("version", "1.0.0"),
        input_contract=dict(data.get("input_contract", {})),
        interaction_contract=dict(data.get("interaction_contract", {})),
        runtime=dict(data.get("runtime", {})),
        hub_integration=dict(data.get("hub_integration", {})),
        extensions={key: value for key, value in data.items() if key not in _CORE_FIELDS},
    )

def load_registry_report(registry_dir: Path | None = None) -> RegistryLoadResult:
    """Return every enabled agent, plus every agent.json that failed to load.

    Unlike `load_registry`, an invalid manifest is not just logged and
    dropped — it comes back as a `RegistryLoadError` so a caller (see
    `/agents-refresh` in orchestrator.py) can expose *why* a specialist isn't
    in the callable set instead of it silently going missing.
    """
    base = registry_dir or AGENT_REGISTRY_DIR
    if not base.exists():
        logger.info("Agent registry directory not found at %s — no agents loaded.", base)
        return RegistryLoadResult(specs=[], errors=[])

    specs: list[AgentSpec] = []
    errors: list[RegistryLoadError] = []
    for agent_dir in sorted(base.iterdir()):
        if not agent_dir.is_dir():
            continue
        spec_file = agent_dir / "agent.json"
        if not spec_file.exists():
            logger.debug("Skipping %s — no agent.json found.", agent_dir.name)
            continue
        try:
            data = json.loads(spec_file.read_text(encoding="utf-8"))
            spec = parse_agent_spec(data)
            specs.append(spec)
            logger.debug("Loaded agent: %s (%s)", spec.id, spec.version)
        except Exception as exc:
            logger.warning("Could not load agent spec from %s: %s", spec_file, exc)
            errors.append(RegistryLoadError(source=str(spec_file), message=str(exc)))

    logger.info(
        "Agent registry: %d enabled agent(s) loaded, %d invalid.", len(specs), len(errors)
    )
    return RegistryLoadResult(specs=specs, errors=errors)


def load_registry(registry_dir: Path | None = None) -> list[AgentSpec]:
    """Return all enabled agents from the agent-factory registry.

    Returns an empty list if the registry directory doesn't exist (e.g., in CI).
    Invalid manifests are skipped; use `load_registry_report` to see why.
    """
    return load_registry_report(registry_dir).specs
