"""Agent registry — reads enabled agents from the agent-factory config directory.

Enabled agents live in agent-factory/config/agents/<id>/agent.json.
The registry loads all of them at startup so the orchestrator can route to them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .config import AGENT_REGISTRY_DIR
from .runtime_policy import validate_runtime_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    """Minimal view of an enabled agent needed for routing."""

    id: str
    name: str
    purpose: str
    aliases: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    backlog_sheet_id: str | None = None
    runtime: dict = field(default_factory=dict)


def load_registry(registry_dir: Path | None = None) -> list[AgentSpec]:
    """Return all enabled agents from the agent-factory registry.

    Returns an empty list if the registry directory doesn't exist (e.g., in CI).
    """
    base = registry_dir or AGENT_REGISTRY_DIR
    if not base.exists():
        logger.info("Agent registry directory not found at %s — no agents loaded.", base)
        return []

    specs: list[AgentSpec] = []
    for agent_dir in sorted(base.iterdir()):
        if not agent_dir.is_dir():
            continue
        spec_file = agent_dir / "agent.json"
        if not spec_file.exists():
            logger.debug("Skipping %s — no agent.json found.", agent_dir.name)
            continue
        try:
            data = json.loads(spec_file.read_text(encoding="utf-8"))
            spec = AgentSpec(
                id=data["id"],
                name=data.get("name", data["id"]),
                purpose=data.get("purpose", ""),
                aliases=data.get("aliases", []),
                tools=data.get("tools", []),
                version=data.get("version", "1.0.0"),
                backlog_sheet_id=data.get("backlog_sheet_id"),
                runtime=data.get("runtime", {}),
            )
            validate_runtime_config(spec.id, spec.runtime)
            specs.append(spec)
            logger.debug("Loaded agent: %s (%s)", spec.id, spec.version)
        except Exception as exc:
            logger.warning("Could not load agent spec from %s: %s", spec_file, exc)

    logger.info("Agent registry: %d enabled agent(s) loaded.", len(specs))
    return specs


def find_agent(registry: list[AgentSpec], query: str) -> AgentSpec | None:
    """Find an agent by ID or alias (case-insensitive)."""
    q = query.strip().lower()
    for spec in registry:
        if spec.id.lower() == q:
            return spec
        if q in [a.lower() for a in spec.aliases]:
            return spec
    return None
