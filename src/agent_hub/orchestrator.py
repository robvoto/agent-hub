"""LangGraph orchestrator — routes tasks to specialist agents via subprocess dispatch."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from .checkpointer import get_checkpointer
from .config import AGENT_FACTORY_ROOT, chat_model_kwargs, configured_model
from .cost_log import extract_usage_metadata, record_llm_run
from .factory_bridge import (
    build_factory_agent_spec,
    invoke_factory_request,
    new_factory_thread_id,
    reject_factory_request,
    relay_factory_build_result,
    resume_factory_request,
)
from .handoff_transition import (
    HandoffEvidenceError,
    HandoffEvidenceResolver,
    HandoffFidelityReview,
    HandoffFidelityReviewer,
    NextTaskContract,
    SourceAwareHandoffEvidenceResolver,
    factory_execution_constraints,
    resolve_approved_design_evidence,
    validate_handoff_references,
)
from .hub_context import HubContextService
from .hub_memory import (
    ExtractionCandidate,
    HubMemoryManager,
    ResourcePromotionCandidate,
    analyze_learning,
    extract_semantic_candidates,
    format_forget_confirmation,
    format_learning_confirmation,
    format_learning_list,
    format_learnings_for_prompt,
)
from .hub_skills import HubSkillStore, SkillProposalResult
from .human_mcp_gateway import get_human_mcp_gateway, load_human_mcp_config
from .human_mcp_tools import make_human_mcp_tools
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
from .project_resources import (
    BACKLOG_RESOURCE_TYPE,
    ProjectResource,
    build_backlog_reference,
    get_project_resource_registry,
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
from .specialist_fanout import FanoutError, run_specialist_fanout
from .specialist_result import SpecialistResultContractError, validate_specialist_result
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


@contextmanager
def _registered_resumed_run(run_id: str):
    """Keep resumed specialist work cancellable through the normal run handle.

    The initial Hub graph invocation registers its run before dispatch, but a
    paused task returns from that invocation and unregisters the handle. Any
    later clarification/decision/approval resume therefore needs a fresh
    handle for the duration of the resumed dispatch so /stop can attach to
    and terminate the live specialist subprocess tree.
    """
    registry = get_task_control_registry()
    registry.register_run(run_id)
    try:
        yield
    finally:
        registry.unregister_run(run_id)


def _project_key_for_session(session_id: str) -> str:
    context = get_project_context_registry().get(session_id)
    return context.project_id if context is not None else DEFAULT_PROJECT_KEY


def _friendly_project_label(project_key: str) -> str:
    return "default" if project_key == DEFAULT_PROJECT_KEY else project_key


_LEARNED_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_LEARNED_REFERENCE_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "and",
        "are",
        "can",
        "from",
        "has",
        "have",
        "here",
        "into",
        "its",
        "just",
        "make",
        "not",
        "our",
        "remove",
        "that",
        "the",
        "this",
        "use",
        "was",
        "with",
        "you",
        "your",
    }
)


def _reference_terms(text: str) -> set[str]:
    without_urls = _LEARNED_URL_RE.sub(" ", text)
    return {
        token
        for token in re.findall(r"[a-z0-9]+", without_urls.lower())
        if len(token) >= 3 and token not in _LEARNED_REFERENCE_STOPWORDS
    }


def _learned_references_for_task(task: str, *, max_items: int = 3) -> list[str]:
    """Recover exact operator-taught URLs when they are clearly relevant.

    The LLM still decides what task to dispatch, but it is not trusted to copy
    durable pointers out of memory. Only active explicit /learn records are
    considered, relevance requires lexical overlap with the dispatched task,
    and the number of recovered pointers is bounded.
    """
    task_terms = _reference_terms(task)
    if not task_terms:
        return []

    ranked: list[tuple[int, float, list[str]]] = []
    for record in HubMemoryManager().list_learnings():
        if record.scope != "operator" or record.status != "active":
            continue
        urls = [value.rstrip(".,;:!?)]}") for value in _LEARNED_URL_RE.findall(record.value)]
        if not urls:
            continue
        overlap = task_terms & _reference_terms(record.value)
        if not overlap:
            continue
        ranked.append((len(overlap), record.created_at.timestamp(), urls))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result: list[str] = []
    for _, _, urls in ranked:
        for url in urls:
            if url not in result:
                result.append(url)
            if len(result) >= max_items:
                return result
    return result


def _human_task_log(task_run_id: str | None, message: str, *args: Any) -> None:
    # The run ID is introduced once when TaskRunStore creates the run and
    # repeated only at terminal/paused checkpoints there. Repeating it on
    # every human-facing line makes the live log harder to scan. Technical
    # logs still carry the full run ID for correlation.
    human_logger.info(message, *args)


_NODE_EXPLANATIONS = {
    "agent": (
        "Understand the request and decide whether Hub should answer directly or call a specialist."
    ),
    "tools": "Execute the selected specialist/tool and return its result to the orchestrator.",
}


def _log_node_intro(node_name: str, explained_nodes: set[str]) -> None:
    if node_name in explained_nodes:
        return
    explanation = _NODE_EXPLANATIONS.get(node_name)
    if explanation is None:
        return
    human_logger.info("Node [%s] — %s", node_name, explanation)
    explained_nodes.add(node_name)


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
    return get_project_context_registry().resolve_for_request(run.session_id, run.user_message)


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


def _promote_learning_resource(
    session_id: str,
    record: Any,
    candidate: ResourcePromotionCandidate | None,
    *,
    source: str,
) -> ProjectResource | None:
    """Validate one explicit-learning resource proposal before persisting it.

    Semantic memory is always stored separately. Promotion is intentionally
    narrower than memory: only high-confidence backlog metadata with the
    fields needed by the existing provider-neutral backlog reference contract
    is accepted, and it is bound to the currently selected canonical project.
    """
    if candidate is None:
        return None
    if candidate.confidence != "high":
        logger.info(
            "Learning %s: resource promotion skipped because confidence was %s.",
            record.identifier,
            candidate.confidence,
        )
        return None
    if candidate.resource_type != BACKLOG_RESOURCE_TYPE:
        logger.info(
            "Learning %s: resource promotion skipped for unsupported type %r.",
            record.identifier,
            candidate.resource_type,
        )
        return None

    location = candidate.location
    if not isinstance(location, dict):
        return None
    spreadsheet_id = location.get("spreadsheet_id")
    sheet_name = location.get("sheet_name")
    if (
        not isinstance(spreadsheet_id, str)
        or not spreadsheet_id.strip()
        or not isinstance(sheet_name, str)
        or not sheet_name.strip()
    ):
        logger.info(
            "Learning %s: backlog resource promotion skipped because spreadsheet_id "
            "and sheet_name were not both supplied.",
            record.identifier,
        )
        return None

    resolution = get_project_context_registry().resolve_for_dispatch(session_id)
    if resolution.error:
        logger.info(
            "Learning %s: resource promotion skipped because the selected project "
            "could not be revalidated: %s",
            record.identifier,
            resolution.error,
        )
        return None
    if resolution.context is None:
        logger.info(
            "Learning %s: resource promotion skipped because no canonical project is selected.",
            record.identifier,
        )
        return None

    memory_source = f"memory:{record.identifier}"
    try:
        return get_project_resource_registry().register_backlog(
            resolution.context,
            location=location,
            source=memory_source,
            provenance=(source, memory_source),
            metadata={
                "memory_id": record.identifier,
                "memory_type": record.type,
                "memory_scope": record.scope,
            },
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "Learning %s: validated resource promotion could not be stored: %s",
            record.identifier,
            exc,
        )
        return None


class RoutingDecision(BaseModel):
    route: Literal["specialist", "direct", "clarify"]
    task_kind: str | None = None
    reason: str


def _advertised_task_kinds(registry: list[AgentSpec]) -> set[str]:
    kinds: set[str] = set()
    for spec in registry:
        kinds.update(spec.task_contract.get("task_kinds", []) or [])
    return kinds


def _eligible_agents_for_task_kind(registry: list[AgentSpec], task_kind: str) -> list[AgentSpec]:
    return [
        spec for spec in registry if task_kind in (spec.task_contract.get("task_kinds", []) or [])
    ]


def _classify_routing_request(
    message: str,
    registry: list[AgentSpec],
    *,
    model: str,
) -> RoutingDecision:
    """Classify against task kinds advertised by the live specialist registry.

    Hub owns only the stable routing outcomes (specialist/direct/clarify). The
    specialist task taxonomy is discovered from manifests at runtime; Hub has
    no agent-name or task-keyword mapping.
    """
    normalized_message = message.casefold()
    human_mcp_config = load_human_mcp_config()
    explicit_human_mcp = "human mcp" in normalized_message
    explicit_tool = any(
        re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", normalized_message)
        for name in human_mcp_config.allowed_tools
    )
    if explicit_human_mcp or explicit_tool:
        return RoutingDecision(
            route="direct",
            task_kind=None,
            reason="Operator explicitly requested a Hub-owned Human MCP runtime capability.",
        )

    advertised = sorted(_advertised_task_kinds(registry))
    if not advertised:
        return RoutingDecision(
            route="direct", task_kind=None, reason="No specialist task kinds are advertised."
        )

    cards = []
    for spec in registry:
        kinds = spec.task_contract.get("task_kinds", []) or []
        if not kinds:
            continue
        descriptions = spec.task_contract.get("task_kind_descriptions", {}) or {}
        kind_lines = [
            f"- {kind}: {descriptions.get(kind, 'No description supplied.')}" for kind in kinds
        ]
        cards.append(
            f"Agent: {spec.name}\n"
            f"Task kinds:\n" + "\n".join(kind_lines) + f"\nPurpose:\n{spec.purpose}"
        )
    prompt = (
        "Classify the operator request for routing.\n"
        "Choose route='specialist' only when exactly one advertised task kind clearly "
        "describes the requested work. "
        "For specialist routing, task_kind MUST be one of the advertised task kinds below. "
        "Use route='direct' for ordinary conversation that does not require a specialist. "
        "Use route='direct' for browser automation, Google Sheets/Docs/Gmail work, or other "
        "Human MCP operations that Hub can perform with its own runtime tools, unless the "
        "operator is explicitly asking to change code or specialist implementation. "
        "Use route='direct' when the operator explicitly asks Hub to fan out or run multiple "
        "independent specialist tasks in parallel across projects; Hub owns that coordination. "
        "Use route='clarify' when the requested work is ambiguous or no advertised task "
        "kind clearly fits. "
        "Do not choose an agent; choose only the task kind.\n\n"
        f"Advertised task kinds: {', '.join(advertised)}\n\n"
        + "\n\n".join(cards)
        + f"\n\nOperator request:\n{message}"
    )
    # Do not force optional sampling parameters here. Some approved models
    # accept only provider defaults, so shared Hub code must stay compatible
    # with every model admitted by runtime configuration.
    llm = ChatOpenAI(**chat_model_kwargs(model)).with_structured_output(RoutingDecision)
    decision = llm.invoke([HumanMessage(content=prompt)])
    if decision.route == "specialist":
        if decision.task_kind not in advertised:
            raise RuntimeError(
                f"Routing classifier returned unsupported task kind: {decision.task_kind!r}."
            )
        if not _eligible_agents_for_task_kind(registry, decision.task_kind):
            raise RuntimeError(
                "Routing classifier selected task kind with no eligible specialist: "
                f"{decision.task_kind!r}."
            )
    elif decision.task_kind is not None:
        raise RuntimeError(f"Routing classifier returned task_kind for route {decision.route!r}.")
    return decision


def _emit_progress_update(update: ProgressUpdate) -> None:
    callback = get_current_progress_callback()
    if callback is None:
        return
    try:
        callback(update)
    except Exception:
        logger.exception("Progress notifier failed for run %s", update.run_id)


_SYSTEM_PROMPT = """You are the Agent Hub orchestrator. You coordinate specialist AI agents.

The specialist tools available on this turn have already passed Hub's manifest-driven
eligibility stage for the classified advertised task kind. If more than one specialist
remains, choose only among those eligible tools. Use their advertised task-contract
capabilities and input/lifecycle contracts as the authority; purpose is descriptive
context, not a substitute for an advertised capability. Do not infer specialist scope
from an agent name, project name, or alias.

A named project is the target of the work, not automatically the specialist.
A request naming a project is not routed to that project's own agent unless
the specialist advertises the task capability being requested.

When a user sends a request:
1. Select only from the already eligible specialist tools for this request.
2. Call that agent's tool with a clear, bounded task description.
3. Return the agent's result to the user.

If the user gives an explicit pointer — a file path, URL, or ID — pass it via
the tool's `references` argument verbatim instead of paraphrasing it into the
task description. Do not interpret what a reference means; only relay it.

Do not explain what you would do — invoke the agent and return the result.
Do not answer coding, research, or creation tasks yourself — that is the specialist agent's job.

If the agent returns a clarification question, relay it to the user verbatim.
If the agent requires approval, tell the user exactly what needs approval and wait.
If the selected specialist asks for clarification, relay it instead of guessing."""


def _build_system_prompt(state: Any) -> list[Any]:
    """Assemble the system prompt fresh per turn, folding in operator-stored learnings.

    Reading the knowledge store on every model call (rather than baking learnings
    into the prompt at graph-construction time) means a /learn or /forget takes
    effect on the very next turn without restarting the hub.
    """
    messages = (
        state.get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    )
    learnings_block = format_learnings_for_prompt(HubMemoryManager().list_learnings())
    content = f"{_SYSTEM_PROMPT}\n\n{learnings_block}" if learnings_block else _SYSTEM_PROMPT
    return [SystemMessage(content=content)] + list(messages)


def _message_preview(message: Any) -> str | None:
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        names = [call.get("name", "unknown-tool") for call in tool_calls if isinstance(call, dict)]
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
    explained_nodes: set[str],
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
                    _truncate(str(data["error"])),
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
            _log_node_intro(node_name, explained_nodes)
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
    treated as accepting the legacy project_root/references keys only."""
    input_contract = spec.input_contract
    if "accepted_context" in input_contract:
        return set(input_contract.get("accepted_context") or [])
    for field_name in ("optional_fields", "optional"):
        declared = input_contract.get(field_name)
        if isinstance(declared, list):
            return set(declared)
    return {"project_root", "references"}


_UNSET_RESOURCE_OVERRIDE = object()


def _resolve_backlog_reference_for_dispatch(
    spec: AgentSpec,
    project_context: ProjectContext | None,
    *,
    task: str,
    references: list[str] | None,
) -> tuple[dict[str, str] | None, str | None]:
    """Resolve one project backlog row only for specialists that advertise it."""
    if "backlog_reference" not in _accepted_context_keys(spec):
        return None, None
    if project_context is None:
        return None, None

    resolution = get_project_resource_registry().resolve_for_request(
        project_context,
        resource_type=BACKLOG_RESOURCE_TYPE,
        request_text=task,
        references=references or (),
    )
    if resolution.error:
        return None, resolution.error
    if resolution.resource is None:
        return None, None
    return build_backlog_reference(resolution.resource, item_id=resolution.item_id), None


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


def _select_governed_skills(task: str) -> list[dict[str, Any]]:
    """Return bounded active Hub skills relevant to a specialist dispatch."""
    skills = HubSkillStore().select_for_dispatch(task)
    return [
        {
            "slug": skill.slug,
            "version": skill.version,
            "title": skill.title,
            "content": skill.body,
        }
        for skill in skills
    ]


def _governed_skill_metadata(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persist only stable skill identity, not duplicated instruction content."""
    return [{"slug": skill["slug"], "version": skill["version"]} for skill in skills]


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
        if isinstance(version, int) and version in HUB_SUPPORTED_PROJECT_CONTEXT_SCHEMA_VERSIONS
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
    execution_constraints: dict[str, Any] | None = None,
    project_root_override: str | None = None,
    project_context_override: ProjectContext | None = None,
    backlog_reference_override: dict[str, str] | None | object = _UNSET_RESOURCE_OVERRIDE,
    task_kind: str | None = None,
    result_registry: list[AgentSpec] | None = None,
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
    if backlog_reference_override is _UNSET_RESOURCE_OVERRIDE:
        backlog_reference, backlog_resource_error = _resolve_backlog_reference_for_dispatch(
            spec,
            project_context,
            task=task,
            references=references,
        )
    else:
        backlog_reference = backlog_reference_override
        backlog_resource_error = None

    project_root, references, missing_context = _resolve_dispatch_context(
        spec, project_root=project_root, references=references
    )
    required_context = set(spec.input_contract.get("required_context") or [])
    if "backlog_reference" in required_context and not backlog_reference:
        missing_context.append("backlog_reference")
    accepted_context = _accepted_context_keys(spec)
    resource_context_accepted = (
        "project_root" in accepted_context or "backlog_reference" in accepted_context
    )
    if project_context_error and resource_context_accepted:
        output = {"status": "failed", "summary": project_context_error}
        _record_agent_status(spec, output, task_run_id)
        return output
    if backlog_resource_error:
        output = {"status": "failed", "summary": backlog_resource_error}
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
    governed_skills = _select_governed_skills(task)
    if governed_skills:
        _human_task_log(
            task_run_id,
            "Applying governed Hub skills: %s",
            ", ".join(f"{skill['slug']}@v{skill['version']}" for skill in governed_skills),
        )

    if task_run_id:
        dispatch_context = {
            "agent_request_id": request_id,
            "runtime_mode": "subprocess",
            "agent_dispatch_project_root": project_root,
            "agent_dispatch_references": references,
            "agent_dispatch_project_id": envelope_project_id,
            "agent_dispatch_project_contract_version": envelope_project_contract_version,
            "agent_dispatch_project_fingerprint": envelope_project_fingerprint,
            "agent_dispatch_backlog_reference": backlog_reference,
            "agent_dispatch_task_kind": task_kind,
            "governed_skills": _governed_skill_metadata(governed_skills),
            "pinned_agent_spec": dataclasses.asdict(spec),
            "pinned_agent_version": spec.version,
            "pinned_agent_fingerprint": spec_fingerprint(spec),
        }
        if execution_constraints is not None:
            dispatch_context["agent_dispatch_execution_constraints"] = execution_constraints
        get_task_run_store().transition(
            task_run_id,
            TASK_STATE_DISPATCHED,
            detail=f"Dispatched task to specialist agent '{spec.id}'.",
            selected_agent_id=spec.id,
            dispatched_task=task,
            context_updates=dispatch_context,
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
        if backlog_reference:
            _human_task_log(
                task_run_id,
                "Passing resolved backlog resource to %s: %s",
                spec.name,
                json.dumps(backlog_reference, sort_keys=True),
            )
        input_data = build_task_envelope(
            task=task,
            request_id=request_id,
            run_id=task_run_id,
            source="agent-hub",
            execution_mode=runtime["default_execution_mode"],
            progress_jsonl=str(progress_file),
            task_kind=task_kind,
            project_root=project_root,
            project_id=envelope_project_id,
            project_contract_version=envelope_project_contract_version,
            project_fingerprint=envelope_project_fingerprint,
            project_context=envelope_project_context,
            references=references,
            backlog_reference=backlog_reference,
            human_approved=human_approved,
            approval_token=approval_token,
            resume=resume,
            decision=decision,
            governed_skills=governed_skills,
            execution_constraints=execution_constraints,
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
        if (handle is not None and handle.cancel_requested) or (
            cancelled_run is not None and cancelled_run.state == TASK_STATE_CANCELLED
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
        stdout_stream = getattr(proc, "stdout", None)
        stderr_stream = getattr(proc, "stderr", None)
        stderr = ""
        try:
            if stdout_stream is None or stderr_stream is None:
                # Compatibility for simple test doubles. Real subprocesses always use
                # PIPE streams and take the concurrent-drain path below.
                while proc.poll() is None:
                    for update in progress_tailer.poll():
                        _emit_progress_update(update)
                    time.sleep(PROGRESS_POLL_INTERVAL_SECONDS)
                for update in progress_tailer.poll(final=True):
                    _emit_progress_update(update)
                progress_tailer.finish()
                communicate = getattr(proc, "communicate", None)
                if callable(communicate):
                    _, stderr = communicate()
            else:
                stream_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
                stderr_lines: list[str] = []

                def _drain_stream(name: str, stream: Any) -> None:
                    try:
                        for line in iter(stream.readline, ""):
                            stream_queue.put((name, line))
                    finally:
                        stream_queue.put((name, None))

                reader_threads = [
                    threading.Thread(
                        target=_drain_stream,
                        args=("stdout", stdout_stream),
                        name=f"{spec.id}-stdout",
                        daemon=True,
                    ),
                    threading.Thread(
                        target=_drain_stream,
                        args=("stderr", stderr_stream),
                        name=f"{spec.id}-stderr",
                        daemon=True,
                    ),
                ]
                for thread in reader_threads:
                    thread.start()

                open_streams = len(reader_threads)
                stdout_progress_seen = False
                while open_streams:
                    try:
                        source, line = stream_queue.get(timeout=PROGRESS_POLL_INTERVAL_SECONDS)
                    except queue.Empty:
                        source = ""
                        line = ""
                        if not stdout_progress_seen:
                            for update in progress_tailer.poll():
                                _emit_progress_update(update)

                    if line is None:
                        open_streams -= 1
                    elif source == "stderr":
                        stderr_lines.append(line.rstrip("\r\n"))
                    elif source == "stdout":
                        update = progress_tailer.process_line(line)
                        if update is not None:
                            stdout_progress_seen = True
                            _emit_progress_update(update)

                    background = progress_tailer.maybe_emit_background_update()
                    if background is not None:
                        _emit_progress_update(background)

                proc.wait()
                for thread in reader_threads:
                    thread.join(timeout=5)
                # Legacy specialists may still write the negotiated progress file.
                # Drain it once at completion without permanent file polling.
                for update in progress_tailer.poll(final=True):
                    _emit_progress_update(update)
                progress_tailer.finish()
                stderr = "\n".join(stderr_lines)
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
        try:
            validate_specialist_result(
                output,
                result_registry if result_registry is not None else [spec],
            )
        except SpecialistResultContractError as exc:
            summary = f"Specialist '{spec.name}' returned an invalid result contract: {exc}"
            logger.warning("Task %s: %s", task_run_id or request_id, summary)
            _human_task_log(
                task_run_id,
                "%s returned an invalid result contract; failing closed: %s",
                spec.name,
                exc,
            )
            output = {
                "status": "failed",
                "summary": summary,
                "result_kind": "contract_failure",
                "caller_action": "inspect_failure",
            }
        if (
            output.get("status")
            not in (
                "needs_clarification",
                "approval_required",
            )
            and _valid_pending_decision(output.get("pending_decision")) is None
        ):
            progress_tailer.mark_unavailable_if_silent()
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
    governed_skills = _select_governed_skills(task) if action == "invoke" else []
    if governed_skills:
        _human_task_log(
            task_run_id,
            "Applying governed Hub skills: %s",
            ", ".join(f"{skill['slug']}@v{skill['version']}" for skill in governed_skills),
        )
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
                "governed_skills": _governed_skill_metadata(governed_skills),
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
            governed_skills=governed_skills,
        )

    cache = get_manifest_cache().get_or_refresh(spec)
    factory_status = result.get("status")
    if factory_status == "waiting_approval" or result.get("interrupted"):
        hub_status = "approval_required"
    elif factory_status in {"failed", "rejected"}:
        hub_status = "failed"
    else:
        hub_status = "success"
    output = {
        "status": hub_status,
        "summary": result.get("summary", result.get("response", "")),
        "factory_status": factory_status,
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
    for field in ("next_task", "artifact_reference"):
        if field in result:
            output[field] = result[field]
    output["factory_result"] = {
        key: result[key]
        for key in ("status", "summary", "next_task", "artifact_reference")
        if key in result
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
    options_text = "\n".join(
        f"{index}. {_describe_decision_option(option)}"
        for index, option in enumerate(pending_decision["options"], start=1)
    )
    return (
        f"[{spec.name}] Decision needed: {prompt}\n"
        f"{options_text}\n"
        "Reply with the option number or name. Add guidance after it if needed."
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

    raise RuntimeError(f"Agent '{spec.id}' returned status '{status}': {summary or output}")


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


def _make_agent_tool(
    spec: AgentSpec,
    *,
    task_kind: str | None = None,
    result_registry: list[AgentSpec] | None = None,
) -> Any:
    """Create a LangChain tool that dispatches to the registered agent."""
    mode = spec.runtime["mode"]
    description = get_manifest_cache().description_for(spec)
    registry_for_results = result_registry if result_registry is not None else [spec]

    def _dispatch_task_with_operator_source(task: str, task_run_id: str | None) -> str:
        """Preserve the exact operator request alongside Hub's bounded reformulation.

        The reformulated task remains the instruction. The verbatim operator request is
        appended as source context so specialist handoffs cannot lose literal contracts,
        evidence, or other payloads while the Hub is summarising the task for routing.
        """

        if not task_run_id:
            return task
        run = get_task_run_store().get_run(task_run_id)
        if run is None:
            return task
        operator_request = run.user_message.strip()
        if not operator_request or operator_request in task:
            return task
        return (
            f"{task.rstrip()}\n\n"
            "ORIGINAL OPERATOR REQUEST (verbatim source context; this does not override "
            "the Hub task, specialist contract, permissions, or safety boundaries):\n"
            f"{operator_request}"
        )

    @lc_tool(spec.id, description=description)
    def _call_agent(task: str, references: list[str] | None = None) -> str:
        resolved_references = (
            references if references is not None else _learned_references_for_task(task)
        )
        if references is None and resolved_references:
            logger.info(
                "Recovered %d relevant reference(s) from active operator learning for %s.",
                len(resolved_references),
                spec.id,
            )
        task_run_id = get_current_task_run_id()
        dispatch_task = _dispatch_task_with_operator_source(task, task_run_id)
        if task_run_id:
            get_task_run_store().transition(
                task_run_id,
                TASK_STATE_ROUTED,
                detail=f"Routed to {spec.name}.",
                selected_agent_id=spec.id,
            )
        if mode == "subprocess":
            return _format_output(
                spec,
                _dispatch_subprocess(
                    spec,
                    dispatch_task,
                    references=(
                        resolved_references
                        if references is not None or resolved_references
                        else None
                    ),
                    task_kind=task_kind,
                    result_registry=registry_for_results,
                ),
            )
        if mode == "factory_brain":
            return _format_output(spec, _dispatch_factory_brain(spec, dispatch_task))
        raise RuntimeError(f"Unsupported runtime mode: {mode}")

    return _call_agent


class FanoutBranchRequest(BaseModel):
    agent_id: str
    task_kind: str
    task: str
    project: str


def _make_parallel_specialist_fanout_tool(
    registry: list[AgentSpec],
    *,
    session_id: str,
) -> Any:
    @lc_tool(
        "parallel_specialist_fanout",
        description=(
            "Run 2-4 explicit specialist branches in parallel and join their results. "
            "Each branch must name agent_id, one task_kind advertised by that specialist, "
            "a task, and a distinct known project. Use only when the operator explicitly "
            "asks Hub to coordinate independent work across multiple projects. "
            "Same-project parallel branches are refused."
        ),
    )
    def _fanout(branches: list[FanoutBranchRequest]) -> str:
        parent_run_id = get_current_task_run_id()
        if not parent_run_id:
            raise FanoutError("Fan-out requires an active Hub task run.")
        parent = get_task_run_store().get_run(parent_run_id)
        if parent is None:
            raise FanoutError(f"Fan-out parent run disappeared: {parent_run_id}")
        if parent.state != TASK_STATE_IN_PROGRESS:
            get_task_run_store().transition(
                parent_run_id,
                TASK_STATE_IN_PROGRESS,
                detail=f"Hub fan-out is coordinating {len(branches)} specialist branch(es).",
                human_log=False,
            )

        def _dispatch(
            spec: AgentSpec,
            task: str,
            task_kind: str,
            project: ProjectContext,
        ) -> dict[str, Any]:
            mode = spec.runtime["mode"]
            if mode == "subprocess":
                return _dispatch_subprocess(
                    spec,
                    task,
                    task_kind=task_kind,
                    result_registry=registry,
                    project_root_override=project.root,
                    project_context_override=project,
                )
            if mode == "factory_brain":
                return _dispatch_factory_brain(spec, task)
            raise RuntimeError(f"Unsupported runtime mode: {mode}")

        results = run_specialist_fanout(
            session_id=session_id,
            parent_run_id=parent_run_id,
            registry=registry,
            branches=[branch.model_dump() for branch in branches],
            dispatch=_dispatch,
            format_output=_format_output,
        )
        return json.dumps(
            {
                "branch_count": len(results),
                "results": results,
            },
            ensure_ascii=False,
        )

    return _fanout


def _build_support_tools(
    store: Any,
    registry: list[AgentSpec],
    *,
    session_id: str,
) -> list[Any]:
    tools = [make_shared_docs_tool(registry)]
    tools.extend(make_human_mcp_tools(get_human_mcp_gateway()))
    tools.append(_make_parallel_specialist_fanout_tool(registry, session_id=session_id))
    return tools


def _graph_interrupts(graph: Any, config: dict[str, Any]) -> list[Any]:
    if not hasattr(graph, "get_state"):
        return []
    snapshot = graph.get_state(config)
    interrupts: list[Any] = []
    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts.extend(getattr(task, "interrupts", ()) or ())
    return interrupts


def _human_mcp_approval_message(value: Any) -> str:
    payload = value if isinstance(value, dict) else {}
    tool = str(payload.get("tool") or "unknown")
    arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    summary = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(summary) > 1200:
        summary = summary[:1200] + "…"
    return (
        f"Approval required for Human MCP tool `{tool}`.\n"
        f"Arguments: {summary}\n"
        "Use /approve to run it or /reject [reason] to cancel it."
    )


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
        model: str | None = None,
        *,
        session_id: str | None = None,
        semantic_extractor: Any = None,
        learning_analyzer: Any = None,
        skill_store: Any = None,
        context_service: Any = None,
        routing_classifier: Any = None,
        handoff_reviewer: Any = None,
        handoff_evidence_resolver: HandoffEvidenceResolver | None = None,
    ) -> None:
        self._model = model or configured_model()
        self._registry = _load_specialists()
        self._registry_errors: list[RegistryLoadError] = _load_registry_errors()
        self._registry_last_refreshed = _utcnow_iso()
        self._session_id = session_id or load_or_create_session_id()
        self._semantic_extractor = semantic_extractor or extract_semantic_candidates
        self._learning_analyzer = learning_analyzer or analyze_learning
        self._skill_store = skill_store or HubSkillStore()
        self._context_service = context_service or HubContextService()
        self._routing_classifier = routing_classifier or _classify_routing_request
        self._handoff_reviewer = (
            handoff_reviewer
            if handoff_reviewer is not None
            else HandoffFidelityReviewer()
        )
        self._handoff_evidence_resolver = (
            handoff_evidence_resolver
            if handoff_evidence_resolver is not None
            else SourceAwareHandoffEvidenceResolver()
        )
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

    def _build_graph(
        self,
        registry: list[AgentSpec] | None = None,
        *,
        include_memory_tools: bool = True,
        task_kind: str | None = None,
    ) -> Mapping[str, Any]:
        store = get_knowledge_store()
        checkpointer = get_checkpointer()
        active_registry = self._registry if registry is None else registry

        registry_for_results = self._registry if registry is not None else active_registry
        agent_tools = [
            _make_agent_tool(
                s,
                task_kind=task_kind,
                result_registry=registry_for_results,
            )
            for s in active_registry
        ]
        support_tools = (
            _build_support_tools(store, self._registry, session_id=self._session_id)
            if include_memory_tools
            else []
        )
        tools = agent_tools + support_tools

        llm = ChatOpenAI(**chat_model_kwargs(self._model))

        agent_names = [spec.id for spec in active_registry]
        logger.info(
            "Building LangGraph react agent with model=%s, tools=%s, memory_tools=%s",
            self._model,
            agent_names,
            [tool.name for tool in support_tools] if include_memory_tools else [],
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

    def reset_all(self) -> str:
        store = get_task_run_store()
        active_or_paused = [
            run
            for run in store.list_runs()
            if is_active_state(run.state) or is_paused_state(run.state)
        ]

        cancelled = 0
        for run in active_or_paused:
            handle = get_task_control_registry().request_cancel(run.id, "Reset all by user")
            current = store.get_run(run.id)
            if current is not None and not is_terminal_state(current.state):
                store.transition(
                    run.id,
                    TASK_STATE_CANCELLED,
                    detail="Human cancelled all active/paused tasks via /reset-all.",
                    selected_agent_id=run.selected_agent_id,
                    final_response="Cancelled by /reset-all.",
                    cancellation_reason="Reset all by user",
                    raw_result={"status": "cancelled", "summary": "Reset all by user"},
                )
                cancelled += 1
            if handle is not None:
                handle.mark_stop_reply_sent()

        self._rotate_session(carry_active_work=False)
        return (
            f"Reset all complete. Cancelled {cancelled} active/paused task(s) and "
            "started a fresh conversation."
        )

    def hub_status(self) -> str:
        """Report the same startup metadata as /new without rotating the session."""
        return self._hub_summary("Agent Hub status")

    def _hub_summary(self, heading: str) -> str:
        learning_enabled = get_learning_mode_registry().is_enabled(self._session_id)
        project = get_project_context_registry().get(self._session_id)
        project_label = project.metadata.get("name", project.root) if project else "none"
        active_run = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        if active_run is None:
            active_label = "none"
        else:
            backlog_reference = active_run.context.get("agent_dispatch_backlog_reference") or {}
            task_identifier = backlog_reference.get("item_id") or active_run.id[:8]
            active_label = f"{task_identifier} — {active_run.state}"
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
        store = get_task_run_store()
        pending = store.get_latest_paused_run(self._session_id, project_key=project_key)
        if pending is not None:
            return pending

        # A request may explicitly target a known project other than the
        # operator's live /project selection. Once that task has dispatched,
        # its pinned context is the safe resume authority. Only consider runs
        # that actually recorded such a pin; older unpinned runs retain the
        # existing project-scoped disambiguation behavior.
        pinned = [
            run
            for run in store.list_runs(self._session_id)
            if run.state
            in {
                TASK_STATE_WAITING_CLARIFICATION,
                TASK_STATE_WAITING_APPROVAL,
                TASK_STATE_WAITING_DECISION,
            }
            and run.context.get("agent_dispatch_project_id")
        ]
        return pinned[-1] if len(pinned) == 1 else None

    def current_run_status(self) -> str:
        run = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        return format_current_run_status(run)

    def tasks_status(self) -> str:
        store = get_task_run_store()
        runs = [
            run
            for run in store.list_runs()
            if is_active_state(run.state) or is_paused_state(run.state)
        ]
        if not runs:
            return "No active or paused tasks."

        lines = ["Active/paused tasks:"]
        for run in sorted(runs, key=lambda item: item.updated_at, reverse=True):
            backlog_reference = run.context.get("agent_dispatch_backlog_reference") or {}
            identifier = backlog_reference.get("item_id") or run.id[:8]
            project_key = run.context.get("target_project") or DEFAULT_PROJECT_KEY
            summary = _truncate(run.user_message, 80)
            relation = ""
            parent_id = run.context.get("fanout_parent_run_id") or run.context.get(
                "handoff_parent_run_id"
            )
            child_ids = run.context.get("fanout_child_ids")
            handoff_child_id = run.context.get("handoff_child_run_id")
            if isinstance(parent_id, str) and parent_id:
                relation = f" | child-of:{parent_id[:8]}"
            elif isinstance(child_ids, list) and child_ids:
                relation = f" | fanout-parent:{len(child_ids)}"
            elif isinstance(handoff_child_id, str) and handoff_child_id:
                relation = f" | handoff-child:{handoff_child_id[:8]}"
            lines.append(
                f"{identifier} | {_friendly_project_label(project_key)} | {run.state}"
                f"{relation} | {summary}"
            )
        return "\n".join(lines)

    def _resolve_task_identifier(self, identifier: str) -> tuple[TaskRun | None, str | None]:
        key = identifier.strip()
        if not key:
            return None, "A task ID is required."
        candidates: list[TaskRun] = []
        for run in get_task_run_store().list_runs():
            resumable = is_active_state(run.state) or is_paused_state(run.state)
            sequential_child = run.context.get("handoff_child_run_id")
            child = (
                get_task_run_store().get_run(sequential_child)
                if isinstance(sequential_child, str)
                else None
            )
            has_active_sequential_child = child is not None and (
                is_active_state(child.state) or is_paused_state(child.state)
            )
            if not resumable and not has_active_sequential_child:
                continue
            backlog_reference = run.context.get("agent_dispatch_backlog_reference") or {}
            backlog_id = str(backlog_reference.get("item_id") or "")
            if run.id == key or run.id.startswith(key) or backlog_id == key:
                candidates.append(run)
        if not candidates:
            return None, f"No active or paused task matches '{key}'."
        if len(candidates) > 1:
            return None, f"Task ID '{key}' is ambiguous. Use /tasks and provide a longer ID."
        return candidates[0], None

    def resume_task(self, identifier: str) -> str:
        run, error = self._resolve_task_identifier(identifier)
        if error:
            return error
        assert run is not None
        if not is_paused_state(run.state):
            return f"Task {run.id[:8]} is {run.state}, not paused."

        current = get_task_run_store().get_latest_active_or_paused_run(self._session_id)
        if current is not None and current.id != run.id:
            return (
                f"This conversation already has task {current.id[:8]} ({current.state}). "
                "Stop it or start a new conversation first."
            )

        attached = get_task_run_store().attach_paused_run_to_session(run.id, self._session_id)
        backlog_reference = attached.context.get("agent_dispatch_backlog_reference") or {}
        task_id = backlog_reference.get("item_id") or attached.id[:8]
        if attached.state == TASK_STATE_WAITING_APPROVAL:
            next_action = "Use /approve or /reject."
        elif attached.state == TASK_STATE_WAITING_DECISION:
            next_action = "Reply with the requested option."
        else:
            next_action = "Reply with the requested clarification."
        return f"Resumed {task_id} — {attached.state}. {next_action}"

    def last_run_status(self) -> str:
        run = get_task_run_store().get_latest_completed_or_failed_run(self._session_id)
        return format_last_run_status(run)

    def learn(self, value: str, *, source: str, category: str | None = None) -> str:
        manager = HubMemoryManager()
        existing_operator = [
            r for r in manager.list_learnings() if r.scope == "operator" and r.status == "active"
        ]
        record = manager.learn(value, source=source, category=category)
        relevant_skills = self._skill_store.find_relevant_skills(value)
        relevant_docs = self._context_service.find_relevant_documentation(value)

        try:
            decision = self._learning_analyzer(
                value, existing_operator, relevant_skills, relevant_docs
            )
        except Exception as exc:
            logger.warning("Learning analysis failed for %r: %s", value, exc)
            return format_learning_confirmation(record, analysis_error=str(exc))

        updated_record = manager.reclassify(record.identifier, memory_type=decision.memory_type)
        if updated_record is not None:
            record = updated_record

        promoted_resource = _promote_learning_resource(
            self._session_id,
            record,
            decision.resource_promotion,
            source=source,
        )

        skill_result = None
        if decision.action_kind == "skill":
            if decision.skill_slug and decision.skill_title and decision.skill_body:
                skill_result = self._skill_store.propose_skill(
                    decision.skill_slug,
                    decision.skill_title,
                    decision.skill_body,
                    source=source,
                    memory_id=record.identifier,
                )
            else:
                skill_result = SkillProposalResult(
                    accepted=False,
                    skill=None,
                    reason=(
                        "Analysis chose 'skill' but did not provide a complete slug/title/body."
                    ),
                )

        logger.info(
            (
                "Learning %s analysed: type=%s action=%s code_change_needed=%s "
                "skills=%s docs=%s skill_result=%s"
            ),
            record.identifier,
            decision.memory_type,
            decision.action_kind,
            decision.code_change_needed,
            [skill.slug for skill in relevant_skills],
            [doc.identifier for doc in relevant_docs],
            None
            if skill_result is None
            else {
                "accepted": skill_result.accepted,
                "reason": skill_result.reason,
                "skill_id": None if skill_result.skill is None else skill_result.skill.identifier,
            },
        )

        return format_learning_confirmation(
            record,
            analysis=decision,
            relevant_skills=relevant_skills,
            relevant_docs=relevant_docs,
            skill_result=skill_result,
            resource_promotion=(
                f"Project resource updated: {promoted_resource.resource_type}."
                if promoted_resource is not None
                else None
            ),
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
        auto_ids = {r.identifier for r in existing_active_auto}
        exact_auto_by_value = {
            " ".join(r.value.lower().split()): r for r in existing_active_auto if r.value.strip()
        }

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
            source = f"auto-extraction (session {session_id[:8]})"
            evidence = [r.id for r in runs]

            def promote_resource(record: Any) -> None:
                promoted = _promote_learning_resource(
                    session_id,
                    record,
                    candidate.resource_promotion,
                    source=source,
                )
                if promoted is not None:
                    human_logger.info(
                        "Learning pass: updated project resource %s from memory %s.",
                        promoted.resource_type,
                        record.identifier,
                    )
                    messages.append(f"\U0001f9ed Resource updated: {promoted.resource_type}")

            exact_match = exact_auto_by_value.get(" ".join(candidate.value.lower().split()))
            if candidate.action == "add" and exact_match is not None:
                record = manager.reinforce_auto_semantic(
                    exact_match.identifier, source=source, evidence=evidence
                )
                if record is not None:
                    human_logger.info(
                        "Learning pass: exact duplicate reinforced %s: %s",
                        record.identifier,
                        record.value,
                    )
                    messages.append(f"\U0001f9e0 Reinforced: {record.value}")
                    promote_resource(record)
                continue
            if candidate.action == "reinforce":
                if not candidate.supersedes_id or candidate.supersedes_id not in auto_ids:
                    logger.warning(
                        "Learning pass: invalid reinforce target %s; skipping candidate: %s",
                        candidate.supersedes_id,
                        _truncate(candidate.value),
                    )
                    continue
                record = manager.reinforce_auto_semantic(
                    candidate.supersedes_id, source=source, evidence=evidence
                )
                if record is None:
                    logger.warning(
                        "Learning pass: reinforce target %s was not eligible; skipping.",
                        candidate.supersedes_id,
                    )
                    continue
                human_logger.info(
                    "Learning pass: reinforced %s: %s", record.identifier, record.value
                )
                messages.append(f"\U0001f9e0 Reinforced: {record.value}")
                promote_resource(record)
                continue
            if candidate.action == "update":
                if not candidate.supersedes_id or candidate.supersedes_id not in auto_ids:
                    logger.warning(
                        "Learning pass: invalid update target %s; skipping candidate: %s",
                        candidate.supersedes_id,
                        _truncate(candidate.value),
                    )
                    continue
                manager.set_status(candidate.supersedes_id, "disabled")
            record = manager.record_auto_semantic(candidate.value, source=source, evidence=evidence)
            human_logger.info("Learning pass: stored %s: %s", record.identifier, record.value)
            messages.append(f"\U0001f9e0 Learned: {record.value}")
            promote_resource(record)

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

    def stop_current_task(
        self, reason: str = "Stopped by user", *, identifier: str | None = None
    ) -> str:
        store = get_task_run_store()
        project_key = _project_key_for_session(self._session_id)
        if identifier:
            run, error = self._resolve_task_identifier(identifier)
            if error:
                return error
        else:
            run = store.get_latest_active_or_paused_run(
                self._session_id,
                project_key=project_key,
            )
            if run is None:
                session_runs = [
                    candidate
                    for candidate in store.list_runs(self._session_id)
                    if is_active_state(candidate.state) or is_paused_state(candidate.state)
                ]
                if len(session_runs) == 1:
                    run = session_runs[0]
                elif len(session_runs) > 1:
                    return (
                        "Multiple tasks are active or paused in this conversation. "
                        "Use /tasks, then /stop <id>."
                    )
        if run is None:
            human_logger.info(
                "Stop requested for project '%s', but no active or paused task was found.",
                _friendly_project_label(project_key),
            )
            return "No task is currently active."
        project_key = run.context.get("target_project") or DEFAULT_PROJECT_KEY

        child_ids = run.context.get("fanout_child_ids")
        handoff_child_id = run.context.get("handoff_child_run_id")
        agent_id = run.selected_agent_id or (
            "hub-fanout" if isinstance(child_ids, list) and child_ids else "unknown-agent"
        )
        fanout_children = 0
        if isinstance(child_ids, list):
            for child_id in child_ids:
                if not isinstance(child_id, str):
                    continue
                fanout_children += 1
                get_task_control_registry().request_cancel(child_id, reason)
                child = store.get_run(child_id)
                if child is not None and not is_terminal_state(child.state):
                    store.transition(
                        child.id,
                        TASK_STATE_CANCELLED,
                        detail=f"Parent fan-out task was cancelled: {reason}",
                        selected_agent_id=child.selected_agent_id,
                        final_response=f"Cancelled because parent run {run.id[:8]} was stopped.",
                        cancellation_reason=reason,
                        raw_result={"status": "cancelled", "summary": reason},
                    )
        sequential_children = []
        sequential_child_seen = False
        if isinstance(handoff_child_id, str) and handoff_child_id:
            child = store.get_run(handoff_child_id)
            sequential_child_seen = child is not None
            if child is not None and not is_terminal_state(child.state):
                sequential_children.append(child)
                get_task_control_registry().request_cancel(child.id, reason)
                current_child = store.get_run(child.id)
                if current_child is not None and not is_terminal_state(current_child.state):
                    store.transition(
                        current_child.id,
                        TASK_STATE_CANCELLED,
                        detail=f"Parent handoff task was cancelled: {reason}",
                        selected_agent_id=current_child.selected_agent_id,
                        final_response=f"Cancelled because parent run {run.id[:8]} was stopped.",
                        cancellation_reason=reason,
                        raw_result={"status": "cancelled", "summary": reason},
                    )
        if is_terminal_state(run.state) and sequential_children:
            confirmation = (
                f"Stopped sequential handoff child {sequential_children[0].id} for parent "
                f"run {run.id}. Parent state remains {run.state}."
            )
        else:
            confirmation = f"Stopped run {run.id} for agent '{agent_id}'. State is now cancelled."
        if fanout_children:
            confirmation += (
                f" Cancellation requested for {fanout_children} fan-out child task(s)."
            )
        if sequential_children:
            confirmation += (
                f" Cancellation requested for sequential handoff child "
                f"{sequential_children[0].id[:8]}."
            )
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
        if isinstance(run.context.get("handoff_parent_run_id"), str):
            parent = store.get_run(run.context["handoff_parent_run_id"])
            if parent is not None:
                cancellation_updates = {
                    "handoff_child_status": "cancelled",
                    "handoff_cancellation_reason": reason,
                }
                if (
                    isinstance(parent.context.get("execution_constraints"), dict)
                    and not is_terminal_state(parent.state)
                ):
                    store.transition(
                        parent.id,
                        TASK_STATE_CANCELLED,
                        detail=f"Factory manufacturing child was cancelled: {reason}",
                        final_response=(
                            "[Hub] Factory manufacturing handoff cancelled before "
                            "validated BuildResult evidence was received."
                        ),
                        cancellation_reason=reason,
                        context_updates=cancellation_updates,
                        raw_result={
                            "status": "cancelled",
                            "summary": "Factory child cancelled before validation.",
                        },
                    )
                else:
                    store.update_run(parent.id, context_updates=cancellation_updates)
        elif sequential_children or (
            isinstance(handoff_child_id, str) and sequential_child_seen
        ):
            store.update_run(
                run.id,
                context_updates={
                    "handoff_child_status": "cancelled",
                    "handoff_cancellation_reason": reason,
                },
            )
        if handle is not None:
            handle.mark_stop_reply_sent()
        return confirmation

    def approve_pending(self, *, progress_notify: Any | None = None) -> str:
        pending = self.pending_run()
        if pending is not None and pending.state == TASK_STATE_WAITING_DECISION:
            if self._waiting_handoff(pending):
                return self._transition_decision("approve", progress_notify=progress_notify)
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        if pending.context.get("hub_graph_interrupt_kind") == "human_mcp_approval":
            return self._resume_hub_graph_approval(pending, approved=True)

        spec = self._require_spec(pending)
        _human_task_log(pending.id, "Approval received. Resuming %s.", spec.name)
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="Human approved the pending task.",
            selected_agent_id=spec.id,
        )
        with (
            _registered_resumed_run(pending.id),
            active_task_run(pending.id, progress_callback=progress_notify),
        ):
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
                    backlog_reference_override=pending.context.get(
                        "agent_dispatch_backlog_reference"
                    ),
                    task_kind=pending.context.get("agent_dispatch_task_kind"),
                    result_registry=self._registry,
                    **(
                        {"execution_constraints": pending.context["execution_constraints"]}
                        if pending.context.get("execution_constraints") is not None
                        else {}
                    ),
                )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def reject_pending(self, reason: str = "Rejected by user") -> str:
        pending = self.pending_run()
        if pending is not None and pending.state == TASK_STATE_WAITING_DECISION:
            if self._waiting_handoff(pending):
                return self._transition_decision("reject", reason)
        if pending is None or pending.state != TASK_STATE_WAITING_APPROVAL:
            return "No task is currently waiting for approval."

        if pending.context.get("hub_graph_interrupt_kind") == "human_mcp_approval":
            self._resume_hub_graph_approval(pending, approved=False)
            response_text = f"Human MCP action rejected: {reason}"
            get_task_run_store().transition(
                pending.id,
                TASK_STATE_FAILED,
                detail=f"Human rejected the pending Human MCP action: {reason}",
                final_response=response_text,
                error_message=reason,
                raw_result={"status": "failed", "summary": reason},
            )
            return response_text

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
            summary = result.get("summary", result.get("response", reason))
            response_text = f"[{spec.name}] {summary}"
            raw_result = {
                "status": "failed",
                "summary": summary,
                "factory_status": result.get("status"),
                "factory_result": {
                    key: result[key]
                    for key in ("status", "summary", "next_task", "artifact_reference")
                    if key in result
                },
            }
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

    def _resume_hub_graph_approval(self, pending: TaskRun, *, approved: bool) -> str:
        thread_id = pending.context.get("hub_graph_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("Paused Human MCP task has no graph thread id.")
        request_graph = self._build_graph([])
        config = {"configurable": {"thread_id": thread_id}}
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_IN_PROGRESS,
            detail=(
                "Human approved the pending Human MCP action."
                if approved
                else "Human rejected the pending Human MCP action."
            ),
        )
        result = None
        with active_task_run(pending.id):
            for event in request_graph.stream(
                Command(resume=approved),
                config=config,
                stream_mode=["updates", "values"],
            ):
                if (
                    isinstance(event, tuple)
                    and len(event) == 2
                    and event[0] == "values"
                    and isinstance(event[1], dict)
                ):
                    result = event[1]
        if not approved:
            return "Human MCP action rejected."
        interrupts = _graph_interrupts(request_graph, config)
        if interrupts:
            value = interrupts[0].value
            reply = _human_mcp_approval_message(value)
            store.transition(
                pending.id,
                TASK_STATE_WAITING_APPROVAL,
                detail="Another Human MCP action requires approval.",
                final_response=reply,
                context_updates={"hub_graph_interrupt": value},
            )
            return reply
        if not result or not result.get("messages"):
            raise RuntimeError("Resumed Hub graph returned no final messages.")
        reply = (
            _relay_specialist_terminal_message(result["messages"])
            or result["messages"][-1].content
        )
        store.transition(
            pending.id,
            TASK_STATE_SUCCEEDED,
            detail="Hub completed the approved Human MCP action.",
            final_response=reply,
        )
        return reply

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
                f"[{spec.name}] Cannot resume: no resume state was recorded for this paused task."
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
        with (
            _registered_resumed_run(pending.id),
            active_task_run(pending.id, progress_callback=progress_notify),
        ):
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
                    backlog_reference_override=pending.context.get(
                        "agent_dispatch_backlog_reference"
                    ),
                    task_kind=pending.context.get("agent_dispatch_task_kind"),
                    result_registry=self._registry,
                    **(
                        {"execution_constraints": pending.context["execution_constraints"]}
                        if pending.context.get("execution_constraints") is not None
                        else {}
                    ),
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
                    project_root_override=pending.context.get("agent_dispatch_project_root"),
                    project_context_override=_project_context_override_from_pending(pending),
                    references=pending.context.get("agent_dispatch_references"),
                    backlog_reference_override=pending.context.get(
                        "agent_dispatch_backlog_reference"
                    ),
                    task_kind=pending.context.get("agent_dispatch_task_kind"),
                    result_registry=self._registry,
                    **(
                        {"execution_constraints": pending.context["execution_constraints"]}
                        if pending.context.get("execution_constraints") is not None
                        else {}
                    ),
                )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def provide_decision_reply(
        self,
        reply: str,
        *,
        actor: str = "human",
        progress_notify: Any | None = None,
    ) -> str:
        """Resume a decision pause from a normal user reply."""
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_DECISION:
            return "No task is currently waiting for a decision."

        if self._waiting_handoff(pending):
            choice, _, decision_text = reply.strip().partition(" ")
            normalized = choice.lower().replace(" ", "_")
            if normalized in {"request_changes", "request"} and decision_text.lower().startswith(
                "changes"
            ):
                normalized = "request_changes"
            if normalized == "approve":
                selected = decision_text.strip() or None
                return self._transition_decision(
                    "approve",
                    specialist_id=selected,
                    progress_notify=progress_notify,
                )
            if normalized in {"request_changes", "reject"}:
                return self._transition_decision(
                    normalized,
                    decision_text,
                    progress_notify=progress_notify,
                )
            return (
                "Choose APPROVE [specialist-id], REQUEST_CHANGES <correction>, "
                "or REJECT <reason>."
            )

        choice, _, decision_text = reply.strip().partition(" ")
        if not choice:
            return "Reply with the option number or name."
        if choice.endswith((".", ")")) and choice[:-1].isdigit():
            choice = choice[:-1]

        pending_decision = pending.context.get("specialist_pending_decision") or {}
        options = [
            opt
            for opt in (pending_decision.get("options") or [])
            if isinstance(opt, dict) and str(opt.get("name", "")).strip()
        ]
        option = choice
        if choice.isdigit():
            index = int(choice) - 1
            if index < 0 or index >= len(options):
                valid = ", ".join(str(i) for i in range(1, len(options) + 1)) or "none"
                return f"'{choice}' is not a valid option number. Valid numbers: {valid}."
            option = str(options[index]["name"])

        return self.provide_decision(
            option,
            decision_text.strip(),
            actor=actor,
            progress_notify=progress_notify,
        )

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
        if self._waiting_handoff(pending):
            return self._transition_decision(
                option,
                text,
                progress_notify=progress_notify,
            )

        spec = self._require_spec(pending)
        pending_decision = pending.context.get("specialist_pending_decision") or {}
        options = pending_decision.get("options") or []
        allowed = {opt["name"] for opt in options if isinstance(opt, dict) and opt.get("name")}
        if option not in allowed:
            valid = ", ".join(sorted(allowed)) or "none"
            return (
                f"[{spec.name}] '{option}' is not a valid option right now. Valid options: {valid}."
            )

        _human_task_log(pending.id, "Decision '%s' received. Resuming %s.", option, spec.name)
        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail=f"User provided decision '{option}' for the paused task.",
            selected_agent_id=spec.id,
        )
        decision = {"option": option, "text": text, "actor": actor}
        # Decision resumes reuse the specialist's existing request checkpoint.
        # The coding backend must load the already-approved Factory controls
        # from that checkpoint rather than receiving them as a new envelope
        # field.  Preserve the approval marker for Factory manufacturing
        # children without re-submitting immutable execution constraints.
        resume_kwargs: dict[str, Any] = {}
        if pending.context.get("execution_constraints") is not None:
            resume_kwargs["human_approved"] = True
        with (
            _registered_resumed_run(pending.id),
            active_task_run(pending.id, progress_callback=progress_notify),
        ):
            output = _dispatch_subprocess(
                spec,
                "",
                request_id=pending.context.get("agent_request_id"),
                decision=decision,
                project_root_override=pending.context.get("agent_dispatch_project_root"),
                project_context_override=_project_context_override_from_pending(pending),
                references=pending.context.get("agent_dispatch_references"),
                backlog_reference_override=pending.context.get("agent_dispatch_backlog_reference"),
                task_kind=pending.context.get("agent_dispatch_task_kind"),
                result_registry=self._registry,
                **resume_kwargs,
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

    @staticmethod
    def _handoff_fingerprint(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _handoff_project_context(self, run: TaskRun) -> ProjectContext | None:
        value = run.context.get("originating_project_context")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise HandoffEvidenceError("originating project context is not a structured object")
        try:
            return ProjectContext.from_dict(value)
        except (KeyError, TypeError, ValueError) as exc:
            raise HandoffEvidenceError("originating project context is incomplete") from exc

    def _handoff_target_project_context(
        self,
        run: TaskRun,
        references: Sequence[str],
        originating_project_context: ProjectContext | None,
    ) -> ProjectContext | None:
        resolver = self._handoff_evidence_resolver
        resolver_method = getattr(resolver, "target_project_context", None)
        if not callable(resolver_method):
            return originating_project_context
        return resolver_method(
            references,
            originating_project_context,
            originating_agent_id=run.selected_agent_id,
        )

    def _resolve_handoff_evidence(
        self,
        references: Sequence[str],
        next_task: NextTaskContract,
        project_context: ProjectContext | None,
        *,
        originating_agent_id: str | None,
        factory_thread_id: str | None,
    ) -> Any:
        resolver = self._handoff_evidence_resolver
        if isinstance(resolver, SourceAwareHandoffEvidenceResolver):
            return resolver.resolve(
                references,
                next_task,
                project_context,
                originating_agent_id=originating_agent_id,
                factory_thread_id=factory_thread_id,
            )
        return resolver.resolve(references, next_task, project_context)

    def _eligible_handoff_choices(
        self, task_kind: str
    ) -> tuple[list[AgentSpec], list[dict[str, str]]]:
        eligible = _eligible_agents_for_task_kind(self._registry, task_kind)
        choices = [
            {"id": spec.id, "name": spec.name, "task_kind": task_kind}
            for spec in eligible
        ]
        return eligible, choices

    @staticmethod
    def _waiting_handoff(pending: TaskRun) -> bool:
        handoff = pending.context.get("hub_transition_decision")
        return isinstance(handoff, dict) and handoff.get("decision") in {
            "waiting",
            "revision_requested",
        }

    def _format_handoff_packet(self, handoff: dict[str, Any]) -> str:
        evidence = handoff["approved_design_evidence"]
        review = handoff["review"]
        target = evidence.get("target_project")
        target_label = (
            target["project_id"] if target else "default specialist project (none selected)"
        )
        selected = handoff.get("resolved_specialist_id")
        resolved = selected or "human choice required from eligible specialists"
        lines = [
            "Factory design completed.",
            "",
            "Proposed implementation handoff:",
            "",
            f"Originating design: {evidence['design_id']}",
            f"Design package: {evidence['package_id']}",
            f"Task kind: {handoff['next_task']['task_kind']}",
            f"Target project: {target_label}"
            + (f" (root: {target['root']})" if target else ""),
            "Implementation task:",
            handoff["next_task"]["task"],
            f"Resolved specialist: {resolved}",
            "Eligible specialists: "
            + ", ".join(
                f"{choice['name']} ({choice['id']})" for choice in handoff["eligible_specialists"]
            ),
            "",
            "Approved design constraints:",
            f"- Purpose: {evidence['purpose']}",
            f"- Permissions: {json.dumps(evidence['permissions'], sort_keys=True)}",
            f"- Runtime: {json.dumps(evidence['runtime'], sort_keys=True)}",
            f"- Budgets/limits: {json.dumps(evidence['budgets'], sort_keys=True)}",
            "- Acceptance criteria: " + "; ".join(evidence["acceptance_criteria"]),
            "- Stop conditions: " + "; ".join(evidence["stop_conditions"]),
            "",
            f"Independent review: {review['verdict']}",
            "Review findings:",
        ]
        if evidence.get("other_constraints"):
            stop_index = next(
                index
                for index, line in enumerate(lines)
                if line.startswith("- Stop conditions:")
            )
            lines.insert(
                stop_index + 1,
                "- Other approved constraints: "
                + json.dumps(evidence["other_constraints"], sort_keys=True),
            )
        for field, finding in review["coverage"].items():
            lines.append(f"- [{finding['result']}] {field}: {finding['detail']}")
        for heading in (
            "omissions",
            "contradictions",
            "unexplained_scope_expansion",
            "unresolved_risks",
            "ambiguity",
        ):
            values = review[heading]
            if values:
                lines.append(f"- {heading}: " + "; ".join(values))
        lines.extend(
            [
                "",
                "References:",
                *[f"- {reference}" for reference in handoff["next_task"].get("references", [])],
                "",
                "Decision:",
                "APPROVE" + (f" <specialist-id: {selected}>" if selected is None else ""),
                "REQUEST CHANGES",
                "REJECT",
            ]
        )
        if selected is None:
            lines.append(
                "For multiple eligible specialists, reply: APPROVE <exact specialist id>."
            )
        return "\n".join(lines)

    def _prepare_handoff_transition(self, run: TaskRun, output: dict[str, Any]) -> str | None:
        """Freeze, review, and persist one exact cross-specialist continuation."""
        if output.get("status") != "success" or "next_task" not in output:
            return None

        depth = run.context.get("cross_specialist_follow_on_depth", 0)
        if not isinstance(depth, int) or depth != 0:
            raise HandoffEvidenceError(
                "cross-specialist follow-on depth must be exactly 0 before the Phase 2 gate"
            )
        try:
            next_task = NextTaskContract.model_validate(output["next_task"])
        except ValidationError as exc:
            raise HandoffEvidenceError("validated next_task could not be reconstructed") from exc

        originating_project_context = self._handoff_project_context(run)
        validate_handoff_references(next_task.references)
        project_context = self._handoff_target_project_context(
            run,
            next_task.references or [],
            originating_project_context,
        )
        authoritative_evidence = self._resolve_handoff_evidence(
            next_task.references or [],
            next_task,
            project_context,
            originating_agent_id=run.selected_agent_id,
            factory_thread_id=run.context.get("agent_thread_id"),
        )
        evidence = resolve_approved_design_evidence(
            authoritative_evidence, next_task, project_context
        )
        execution_constraints = None
        if run.selected_agent_id == "agent-factory":
            execution_constraints = factory_execution_constraints(evidence)
            if execution_constraints is None:
                raise HandoffEvidenceError(
                    "Factory manufacturing evidence has no explicit execution identity"
                )
        eligible, choices = self._eligible_handoff_choices(next_task.task_kind)
        if not eligible:
            raise HandoffEvidenceError(
                f"task_kind {next_task.task_kind!r} is no longer routable in the live registry"
            )
        review = self._handoff_reviewer.review(
            evidence,
            next_task,
            project_context,
            choices,
        )
        if not isinstance(review, HandoffFidelityReview):
            review = HandoffFidelityReview.model_validate(review)

        handoff = {
            "schema_version": 1,
            "originating_run_id": run.id,
            "originating_agent_id": run.selected_agent_id,
            "follow_on_depth": 1,
            "next_task": next_task.model_dump(mode="json"),
            "project_context": project_context.to_dict() if project_context else None,
            "originating_project_context": (
                originating_project_context.to_dict()
                if originating_project_context
                else None
            ),
            "eligible_specialists": choices,
            "eligible_specialist_specs": [dataclasses.asdict(spec) for spec in eligible],
            "eligible_specialist_fingerprints": {
                spec.id: spec_fingerprint(spec) for spec in eligible
            },
            "resolved_specialist_id": eligible[0].id if len(eligible) == 1 else None,
            "approved_design_evidence": evidence.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "decision": "waiting",
        }
        if execution_constraints is not None:
            handoff["execution_constraints"] = execution_constraints
        handoff["fingerprint"] = self._handoff_fingerprint(handoff)
        packet = self._format_handoff_packet(handoff)
        get_task_run_store().transition(
            run.id,
            TASK_STATE_WAITING_DECISION,
            detail="Hub paused for human approval of the evidence-backed implementation handoff.",
            selected_agent_id=run.selected_agent_id,
            final_response=packet,
            context_updates={"hub_transition_decision": handoff},
        )
        _human_task_log(run.id, "Hub is waiting for a human decision on the frozen handoff.")
        return packet

    def _handoff_failure(self, run: TaskRun, error: Exception) -> str:
        message = f"[Hub] Proposed implementation handoff blocked: {error}"
        get_task_run_store().transition(
            run.id,
            TASK_STATE_FAILED,
            detail="Hub refused the proposed handoff because evidence or review validation failed.",
            final_response=message,
            error_message=str(error),
            raw_result={"status": "failed", "summary": str(error)},
        )
        _human_task_log(run.id, "Handoff transition failed closed: %s", error)
        return message

    def _stored_handoff(self, pending: TaskRun) -> dict[str, Any] | None:
        handoff = pending.context.get("hub_transition_decision")
        if not isinstance(handoff, dict):
            return None
        fingerprint = handoff.get("fingerprint")
        if not isinstance(fingerprint, str):
            raise HandoffEvidenceError("persisted handoff has no fingerprint")
        unsigned = dict(handoff)
        unsigned.pop("fingerprint", None)
        if fingerprint != self._handoff_fingerprint(unsigned):
            raise HandoffEvidenceError(
                "the persisted handoff changed after review; a new checkpoint is required"
            )
        if handoff.get("decision") not in {"waiting", "revision_requested"}:
            raise HandoffEvidenceError("the persisted handoff is no longer awaiting a decision")
        return handoff

    def _transition_decision(
        self,
        decision: str,
        text: str = "",
        *,
        specialist_id: str | None = None,
        progress_notify: Any | None = None,
    ) -> str:
        pending = self.pending_run()
        if pending is None or pending.state != TASK_STATE_WAITING_DECISION:
            return "No task is currently waiting for a decision."
        try:
            handoff = self._stored_handoff(pending)
        except HandoffEvidenceError as exc:
            return self._handoff_failure(pending, exc)
        if handoff is None:
            return self.provide_decision(decision, text, progress_notify=progress_notify)

        normalized = decision.strip().lower().replace(" ", "_")
        if normalized not in {"approve", "request_changes", "reject"}:
            return "Choose APPROVE, REQUEST CHANGES, or REJECT for the proposed handoff."
        if handoff.get("decision") == "revision_requested" and normalized != "reject":
            return (
                "The originating design workflow must provide a revised handoff before "
                "approval can continue. No implementation dispatch occurred."
            )
        if normalized == "approve":
            choices = handoff["eligible_specialists"]
            allowed_ids = {choice["id"] for choice in choices}
            selected = specialist_id or handoff.get("resolved_specialist_id")
            if selected is None:
                return (
                    "Multiple specialists are eligible. Reply with APPROVE followed by one exact "
                    "eligible specialist id."
                )
            if selected not in allowed_ids:
                return f"'{selected}' is not an eligible specialist for this handoff."
            try:
                return self._approve_handoff(pending, handoff, selected, progress_notify)
            except TaskCancelled:
                raise
            except Exception as exc:
                return self._handoff_failure(pending, exc)

        store = get_task_run_store()
        stored_decision = dict(handoff)
        stored_decision["decision"] = normalized
        stored_decision["decision_text"] = text
        if normalized == "request_changes":
            if not text.strip():
                return (
                    "Please describe the change you want the originating design workflow "
                    "to make."
                )
            stored_decision["decision"] = "revision_requested"
            stored_decision["revision_status"] = "waiting_for_originating_design_workflow"
            stored_decision["requested_correction"] = text
            unsigned = dict(stored_decision)
            unsigned.pop("fingerprint", None)
            stored_decision["fingerprint"] = self._handoff_fingerprint(unsigned)
            store.update_run(
                pending.id,
                final_response=(
                    "Human requested changes. The originating design workflow remains paused "
                    "for a revised handoff; no implementation dispatch occurred."
                    + (f"\nCorrection: {text}" if text else "")
                ),
                context_updates={
                    "hub_transition_decision": stored_decision,
                    "handoff_requested_correction": text,
                    "handoff_revision_status": "waiting_for_originating_design_workflow",
                },
                raw_result={"status": "request_changes", "summary": text},
            )
            try:
                return self._return_handoff_for_revision(pending, text, progress_notify)
            except TaskCancelled:
                raise
            except Exception as exc:
                return self._handoff_failure(pending, exc)

        store.transition(
            pending.id,
            TASK_STATE_CANCELLED,
            detail="Human rejected the proposed implementation handoff.",
            final_response=(
                "Proposed implementation handoff rejected."
                + (f" Reason: {text}" if text else "")
            ),
            cancellation_reason=text or "Rejected by user",
            context_updates={"hub_transition_decision": stored_decision},
            raw_result={"status": "rejected", "summary": text},
        )
        return (
            store.get_run(pending.id).final_response
            or "Proposed implementation handoff rejected."
        )

    def _return_handoff_for_revision(
        self,
        pending: TaskRun,
        correction: str,
        progress_notify: Any | None,
    ) -> str:
        """Re-enter the originating Factory thread with the human's correction."""
        spec = self._require_spec(pending)
        if spec.runtime.get("mode") != "factory_brain":
            raise HandoffEvidenceError(
                "the originating design workflow does not expose the Hub Factory continuation"
            )
        thread_id = pending.context.get("agent_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise HandoffEvidenceError(
                "the originating Factory workflow has no resumable thread checkpoint"
            )

        store = get_task_run_store()
        store.transition(
            pending.id,
            TASK_STATE_ROUTED,
            detail="Returning the requested handoff correction to the originating design workflow.",
            selected_agent_id=spec.id,
            dispatched_task=correction,
            context_updates={
                "handoff_revision_status": "returned_to_originating_design_workflow",
                "handoff_requested_correction": correction,
            },
        )
        _human_task_log(
            pending.id,
            "Returning the requested handoff correction to %s for revision.",
            spec.name,
        )
        with (
            _registered_resumed_run(pending.id),
            active_task_run(pending.id, progress_callback=progress_notify),
        ):
            output = _dispatch_factory_brain(
                spec,
                correction,
                thread_id=thread_id,
                action="invoke",
            )
        if output.get("status") == "success" and "next_task" not in output:
            raise HandoffEvidenceError(
                "the originating Factory revision returned success without a fresh next_task"
            )
        return self._finalize_specialist_follow_up(pending.id, spec, output)

    def _approve_handoff(
        self,
        pending: TaskRun,
        handoff: dict[str, Any],
        specialist_id: str,
        progress_notify: Any | None,
    ) -> str:
        self._reconcile_registry()
        spec = next((item for item in self._registry if item.id == specialist_id), None)
        if spec is None:
            raise HandoffEvidenceError(
                f"'{specialist_id}' is no longer registered; the handoff checkpoint is invalid"
            )
        next_task = NextTaskContract.model_validate(handoff["next_task"])
        if next_task.task_kind not in (spec.task_contract.get("task_kinds", []) or []):
            raise HandoffEvidenceError(
                f"'{specialist_id}' no longer advertises task_kind {next_task.task_kind!r}; "
                "the handoff checkpoint is invalid"
            )
        expected_fingerprint = (handoff.get("eligible_specialist_fingerprints") or {}).get(
            specialist_id
        )
        if not isinstance(expected_fingerprint, str):
            raise HandoffEvidenceError(
                "the handoff checkpoint has no reviewed specialist fingerprint"
            )
        actual_fingerprint = spec_fingerprint(spec)
        if actual_fingerprint != expected_fingerprint:
            raise HandoffEvidenceError(
                f"specialist '{specialist_id}' changed since review; a fresh handoff "
                "review and approval are required"
            )
        project_data = handoff.get("project_context")
        project_context = ProjectContext.from_dict(project_data) if project_data else None
        if project_context is not None:
            project_resolution = get_project_context_registry().revalidate_context(project_context)
            if project_resolution.error or project_resolution.context != project_context:
                raise HandoffEvidenceError(
                    project_resolution.error
                    or "the frozen target project changed since review; a fresh handoff "
                    "review and approval are required"
                )
        store = get_task_run_store()
        approved = dict(handoff)
        approved["decision"] = "approved"
        approved["resolved_specialist_id"] = specialist_id
        child = store.create_run(session_id=pending.session_id, user_message=next_task.task)
        approved["child_run_id"] = child.id
        unsigned = dict(approved)
        unsigned.pop("fingerprint", None)
        approved["fingerprint"] = self._handoff_fingerprint(unsigned)
        execution_constraints = approved.get("execution_constraints")
        factory_handoff = execution_constraints is not None
        if factory_handoff and not isinstance(pending.context.get("agent_thread_id"), str):
            raise HandoffEvidenceError(
                "Factory manufacturing handoff has no originating Factory thread"
            )
        child_context = {
            "target_project": pending.context.get("target_project"),
            "originating_project_context": pending.context.get("originating_project_context"),
            "cross_specialist_follow_on_depth": 1,
            "handoff_parent_run_id": pending.id,
            "handoff_originating_run_id": handoff.get("originating_run_id", pending.id),
            "handoff_parent_decision": approved,
        }
        if factory_handoff:
            child_context.update(
                {
                    "execution_constraints": execution_constraints,
                    "handoff_factory_thread_id": pending.context["agent_thread_id"],
                }
            )
        store.update_run(child.id, context_updates=child_context)
        store.transition(
            child.id,
            TASK_STATE_ROUTED,
            detail=(
                f"Hub created implementation child run from approved Factory parent "
                f"'{pending.id}'."
            ),
            selected_agent_id=specialist_id,
            dispatched_task=next_task.task,
            context_updates=child_context,
        )
        approval_updates = {
            "hub_transition_decision": approved,
            "handoff_child_run_id": child.id,
        }
        if factory_handoff:
            store.transition(
                pending.id,
                TASK_STATE_IN_PROGRESS,
                detail=(
                    f"Human approved the Factory manufacturing handoff; implementation child "
                    f"run '{child.id}' is awaiting validated Factory evidence."
                ),
                context_updates=approval_updates,
                raw_result={"status": "handoff_approved", "child_run_id": child.id},
            )
        else:
            store.transition(
                pending.id,
                TASK_STATE_SUCCEEDED,
                detail=(
                    f"Human approved the handoff; implementation child run '{child.id}' "
                    "was created."
                ),
                context_updates=approval_updates,
                raw_result={"status": "handoff_approved", "child_run_id": child.id},
            )
        with (
            _registered_resumed_run(child.id),
            active_task_run(child.id, progress_callback=progress_notify),
        ):
            try:
                if spec.runtime["mode"] == "subprocess":
                    dispatch_kwargs = {
                        "references": next_task.references,
                        "project_root_override": project_context.root if project_context else "",
                        "project_context_override": project_context,
                        "backlog_reference_override": None,
                        "task_kind": next_task.task_kind,
                        "result_registry": self._registry,
                    }
                    if factory_handoff:
                        dispatch_kwargs["execution_constraints"] = execution_constraints
                        dispatch_kwargs["human_approved"] = True
                    output = _dispatch_subprocess(spec, next_task.task, **dispatch_kwargs)
                elif spec.runtime["mode"] == "factory_brain":
                    output = _dispatch_factory_brain(spec, next_task.task)
                else:
                    raise RuntimeError(f"Unsupported runtime mode: {spec.runtime['mode']}")
            except TaskCancelled:
                cancellation_message = (
                    "Approved implementation child run was cancelled. No further handoff "
                    "dispatch occurred."
                )
                current_child = store.get_run(child.id)
                if current_child is not None and not is_terminal_state(current_child.state):
                    try:
                        store.transition(
                            child.id,
                            TASK_STATE_CANCELLED,
                            detail=cancellation_message,
                            final_response=cancellation_message,
                            cancellation_reason="Stopped by user",
                            raw_result={"status": "cancelled", "summary": "Stopped by user"},
                        )
                    except ValueError:
                        latest_child = store.get_run(child.id)
                        if latest_child is None or not is_terminal_state(latest_child.state):
                            raise
                if factory_handoff:
                    self._fail_factory_handoff(
                        child.id,
                        cancellation_message,
                        output={"status": "cancelled", "summary": cancellation_message},
                        cancelled=True,
                    )
                else:
                    store.update_run(pending.id, final_response=cancellation_message)
                raise
            except Exception as exc:
                failure_message = f"[Hub] Follow-on implementation child failed: {exc}"
                current_child = store.get_run(child.id)
                if current_child is not None and not is_terminal_state(current_child.state):
                    store.transition(
                        child.id,
                        TASK_STATE_FAILED,
                        detail=f"Implementation child run failed: {exc}",
                        final_response=failure_message,
                        error_message=str(exc),
                        raw_result={"status": "failed", "summary": str(exc)},
                    )
                if factory_handoff:
                    self._fail_factory_handoff(child.id, failure_message, output=output)
                else:
                    store.update_run(pending.id, final_response=failure_message)
                return failure_message
        reply = self._finalize_specialist_follow_up(child.id, spec, output)
        store.update_run(pending.id, final_response=reply)
        return reply

    def invoke(
        self,
        message: str,
        *,
        progress_notify: Any | None = None,
        request_started_at: datetime | None = None,
    ) -> str:
        logger.info("Received user request: %s", message)
        logger.debug("Invoking graph with session_id=%s", self._session_id)
        self._reconcile_registry()
        task_store = get_task_run_store()
        routing = self._routing_classifier(message, self._registry, model=self._model)
        eligible_registry = self._registry
        if routing.route == "specialist":
            assert routing.task_kind is not None
            eligible_registry = _eligible_agents_for_task_kind(self._registry, routing.task_kind)
            human_logger.info(
                "Routing classified request as '%s'; eligible specialist(s): %s.",
                routing.task_kind,
                ", ".join(spec.name for spec in eligible_registry),
            )
        elif routing.route == "clarify":
            human_logger.info("Routing needs clarification: %s", routing.reason)
        else:
            human_logger.info("Routing classified request as direct Hub conversation.")
        if routing.route == "specialist":
            # Once eligibility says this is specialist work, support/context tools
            # must not compete with specialist dispatch. The routing graph receives
            # only the eligible specialist tools; shared_docs remains a Hub-direct
            # support tool for non-specialist turns.
            request_graph = self._build_graph(
                eligible_registry,
                include_memory_tools=False,
                task_kind=routing.task_kind,
            )
        else:
            # Direct/clarification turns must not retain specialist tools after
            # the eligibility stage says no specialist should be dispatched.
            # When the registry is already empty, the base graph is already safe.
            request_graph = self._graph if not self._registry else self._build_graph([])

        project_resolution = get_project_context_registry().resolve_for_request(
            self._session_id, message
        )
        if project_resolution.error:
            return (
                f"I cannot safely resolve the project for this request. {project_resolution.error}"
            )
        project_key = (
            project_resolution.context.project_id
            if project_resolution.context is not None
            else _project_key_for_session(self._session_id)
        )
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
        lifecycle_started_at = request_started_at or datetime.now(timezone.utc)
        task_store.update_run(
            task_run.id,
            context_updates={
                "target_project": project_key,
                "originating_project_context": (
                    project_resolution.context.to_dict()
                    if project_resolution.context is not None
                    else None
                ),
                "cross_specialist_follow_on_depth": 0,
                "request_started_at": lifecycle_started_at.astimezone(timezone.utc).isoformat(),
            },
        )
        _human_task_log(
            task_run.id,
            "Hub is deciding how to handle this request for project '%s'.",
            _friendly_project_label(project_key),
        )
        thread_id = f"{self._session_id}:{project_key}"
        logger.debug("Task %s: project=%s thread_id=%s", task_run.id[:8], project_key, thread_id)
        _repair_dangling_tool_calls(
            request_graph, thread_id, "Interrupted before the specialist could reply."
        )
        config = {"configurable": {"thread_id": thread_id}}
        usage_cb = UsageMetadataCallbackHandler()
        run_config = {**config, "callbacks": [usage_cb]}
        started_at = time.perf_counter()
        get_task_control_registry().register_run(task_run.id)
        graph_path: list[str] = []
        graph_steps: list[tuple[str, str]] = []
        graph_trace_emitted = False
        explained_nodes: set[str] = set()
        try:
            with active_task_run(task_run.id, progress_callback=progress_notify):
                if hasattr(request_graph, "stream"):
                    result = None
                    last_node = None
                    for event in request_graph.stream(
                        {"messages": [HumanMessage(content=message)]},
                        config=run_config,
                        stream_mode=["tasks", "updates", "values"],
                    ):
                        last_node, streamed_values = _consume_graph_stream_event(
                            task_run.id, event, last_node, graph_path, graph_steps, explained_nodes
                        )
                        if streamed_values is not None:
                            result = streamed_values
                    interrupts = _graph_interrupts(request_graph, config)
                    if interrupts:
                        value = interrupts[0].value
                        if not isinstance(value, dict) or value.get("kind") != "human_mcp_approval":
                            raise RuntimeError(f"Unsupported Hub graph interrupt: {value!r}")
                        reply = _human_mcp_approval_message(value)
                        task_store.transition(
                            task_run.id,
                            TASK_STATE_WAITING_APPROVAL,
                            detail=(
                                "Human approval required before executing "
                                f"Human MCP tool '{value.get('tool', 'unknown')}'."
                            ),
                            final_response=reply,
                            context_updates={
                                "hub_graph_interrupt_kind": "human_mcp_approval",
                                "hub_graph_thread_id": thread_id,
                                "hub_graph_interrupt": value,
                            },
                        )
                        return reply
                    if result is None:
                        raise RuntimeError("Orchestrator stream returned no final state.")
                else:
                    result = request_graph.invoke(
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
            if current.state == TASK_STATE_CANCELLED:
                raise TaskCancelled(current.cancellation_reason or "Stopped by user")

            if (
                is_active_state(current.state)
                and isinstance(current.raw_result, dict)
                and current.raw_result.get("status") == "success"
                and "next_task" in current.raw_result
            ):
                try:
                    reply = self._prepare_handoff_transition(current, current.raw_result)
                except Exception as exc:
                    reply = self._handoff_failure(current, exc)
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
        current = get_task_run_store().get_run(run_id)
        if (
            current is not None
            and is_active_state(current.state)
            and output.get("status") == "success"
            and "next_task" in output
        ):
            try:
                packet = self._prepare_handoff_transition(current, output)
            except Exception as exc:
                return self._handoff_failure(current, exc)
            if packet is not None:
                return packet
        factory_receipt = None
        if current is not None and self._is_factory_child(current):
            final_execution_result = (
                current.state in {TASK_STATE_SUCCEEDED, TASK_STATE_FAILED}
                or (
                    is_active_state(current.state)
                    and output.get("status") in {"success", "failed"}
                    and _valid_pending_decision(output.get("pending_decision")) is None
                )
            )
            if final_execution_result:
                try:
                    factory_receipt = self._relay_factory_build_result(current, output)
                except Exception as exc:
                    return self._fail_factory_handoff(
                        run_id,
                        f"[Hub] Factory manufacturing handoff failed closed: {exc}",
                        output=output,
                    )
                audit = self._factory_audit(current, output, receipt=factory_receipt)
                child_raw = dict(current.raw_result or {})
                child_raw.update(audit)
                get_task_run_store().update_run(
                    run_id,
                    context_updates=audit,
                    raw_result=child_raw,
                )
        reply = _format_output(spec, output)
        store = get_task_run_store()
        current = store.get_run(run_id)
        if current is None:
            raise RuntimeError(f"Task run disappeared: {run_id}")
        if current.state == TASK_STATE_CANCELLED:
            raise TaskCancelled(current.cancellation_reason or "Stopped by user")
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
        if factory_receipt is not None:
            self._complete_factory_parent(current, reply, output, factory_receipt)
        return reply

    @staticmethod
    def _is_factory_child(run: TaskRun) -> bool:
        return isinstance(run.context.get("execution_constraints"), dict) and isinstance(
            run.context.get("handoff_parent_run_id"), str
        )

    def _relay_factory_build_result(self, child: TaskRun, output: dict) -> dict[str, Any]:
        build_result = output.get("build_result")
        if not isinstance(build_result, dict):
            raise HandoffEvidenceError(
                "ATL terminal implementation result has no build_result"
            )
        constraints = child.context.get("execution_constraints")
        if not isinstance(constraints, dict):
            raise HandoffEvidenceError("Factory child has no frozen execution constraints")
        thread_id = child.context.get("handoff_factory_thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise HandoffEvidenceError("Factory handoff has no originating Factory thread")
        artifact_reference = constraints.get("artifact_reference")
        if not isinstance(artifact_reference, str) or not artifact_reference:
            raise HandoffEvidenceError("Factory handoff has no frozen BUILD_TASK reference")
        return relay_factory_build_result(
            working_directory=str(AGENT_FACTORY_ROOT),
            thread_id=thread_id,
            artifact_reference=artifact_reference,
            build_result=build_result,
        )

    def _factory_audit(
        self,
        child: TaskRun,
        output: dict,
        *,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        audit = {
            "factory_build_result_reference": (
                child.context.get("execution_constraints", {}).get("artifact_reference")
                if isinstance(child.context.get("execution_constraints"), dict)
                else None
            ),
            "factory_validation_receipt": receipt,
            "factory_build_result": output.get("build_result"),
        }
        if error:
            audit["factory_validation_error"] = error
        return audit

    def _fail_factory_handoff(
        self,
        child_id: str,
        message: str,
        *,
        output: dict,
        cancelled: bool = False,
    ) -> str:
        store = get_task_run_store()
        child = store.get_run(child_id)
        if child is None:
            return message
        audit = self._factory_audit(child, output, error=message)
        child_raw = dict(child.raw_result or {})
        child_raw.update(audit)
        if is_active_state(child.state):
            store.transition(
                child.id,
                TASK_STATE_FAILED,
                detail=message,
                final_response=message,
                error_message=message,
                context_updates=audit,
                raw_result=child_raw,
            )
        else:
            store.update_run(
                child.id,
                context_updates=audit,
                raw_result=child_raw,
                final_response=message,
                error_message=message,
            )
        parent_id = child.context.get("handoff_parent_run_id")
        parent = store.get_run(parent_id) if isinstance(parent_id, str) else None
        if parent is not None and not is_terminal_state(parent.state):
            parent_raw = dict(parent.raw_result or {})
            parent_raw.update(audit)
            parent_raw["status"] = "cancelled" if cancelled else "failed"
            parent_state = TASK_STATE_CANCELLED if cancelled else TASK_STATE_FAILED
            store.transition(
                parent.id,
                parent_state,
                detail=message,
                final_response=message,
                error_message=message if not cancelled else None,
                cancellation_reason=message if cancelled else None,
                context_updates=audit,
                raw_result=parent_raw,
            )
        return message

    def _complete_factory_parent(
        self,
        child: TaskRun,
        reply: str,
        output: dict,
        receipt: dict[str, Any],
    ) -> None:
        parent_id = child.context.get("handoff_parent_run_id")
        if not isinstance(parent_id, str):
            raise HandoffEvidenceError("Factory child has no handoff parent")
        store = get_task_run_store()
        parent = store.get_run(parent_id)
        if parent is None or is_terminal_state(parent.state):
            raise HandoffEvidenceError("Factory handoff parent is unavailable for validation")
        audit = self._factory_audit(child, output, receipt=receipt)
        store.transition(
            parent.id,
            TASK_STATE_SUCCEEDED,
            detail="Factory validated the terminal ATL BuildResult.",
            final_response=reply,
            context_updates=audit,
            raw_result={
                "status": "validated",
                "child_run_id": child.id,
                **audit,
            },
        )

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
