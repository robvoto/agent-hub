"""LangGraph orchestrator — routes tasks to specialist agents via subprocess dispatch."""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool as lc_tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .checkpointer import get_checkpointer
from .config import DEFAULT_MODEL
from .knowledge_store import get_knowledge_store
from .registry import AgentSpec, load_registry

logger = logging.getLogger(__name__)

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


def _dispatch_subprocess(spec: AgentSpec, task: str) -> str:
    """Invoke a subprocess agent, return its output as a formatted string.

    Raises RuntimeError on process failure, missing output, or hard error status.
    """
    runtime = spec.runtime
    entrypoint = runtime.get("entrypoint")
    working_dir = runtime.get("working_directory")

    if not entrypoint:
        raise RuntimeError(
            f"Agent '{spec.id}' has no entrypoint in runtime config. "
            "Set runtime.entrypoint in agent.json."
        )

    request_id = str(uuid.uuid4())

    with tempfile.TemporaryDirectory() as tmpdir:
        input_file = Path(tmpdir) / "input.json"
        output_file = Path(tmpdir) / "output.json"

        input_data = {
            "request_id": request_id,
            "task": task,
            "execution_mode": runtime.get("default_execution_mode", "instruction_only"),
        }
        input_file.write_text(json.dumps(input_data, indent=2), encoding="utf-8")

        input_arg = runtime.get("input_arg", "--input-json")
        output_arg = runtime.get("output_arg", "--output-json")
        cmd = entrypoint.split() + [input_arg, str(input_file), output_arg, str(output_file)]

        logger.info("Dispatching to %s (request_id=%s): %s", spec.id, request_id, task[:120])

        proc = subprocess.run(
            cmd,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if not output_file.exists():
            raise RuntimeError(
                f"Agent '{spec.id}' subprocess (exit={proc.returncode}) wrote no output.\n"
                f"stderr: {proc.stderr.strip()}"
            )

        output = json.loads(output_file.read_text(encoding="utf-8"))
        return _format_output(spec, output)


def _format_output(spec: AgentSpec, output: dict) -> str:
    """Convert agent output JSON to a string for the orchestrator LLM."""
    status = output.get("status", "unknown")
    summary = output.get("summary", "")

    if status == "success":
        instruction = output.get("coding_agent_instruction", "")
        return f"[{spec.name}] {summary}\n\n{instruction}".strip()

    if status == "needs_clarification":
        return f"[{spec.name}] Clarification needed: {summary}"

    if status == "approval_required":
        token = output.get("approval_token", "")
        return (
            f"[{spec.name}] Approval required: {summary}\n"
            f"Approval token: {token}\n"
            "Resubmit with human_approved=true and this token once approved."
        )

    raise RuntimeError(
        f"Agent '{spec.id}' returned status '{status}': {summary or output}"
    )


def _make_agent_tool(spec: AgentSpec) -> Any:
    """Create a LangChain tool that dispatches to the registered agent."""
    mode = spec.runtime.get("mode", "manual")

    @lc_tool(spec.id, description=f"{spec.name}: {spec.purpose}")
    def _call_agent(task: str) -> str:
        if mode == "subprocess":
            return _dispatch_subprocess(spec, task)
        raise NotImplementedError(
            f"Agent '{spec.id}' runtime mode is '{mode}'. "
            "Only 'subprocess' agents can be invoked. "
            "Update runtime.mode and runtime.entrypoint in agent.json."
        )

    return _call_agent


def _build_memory_tools(store: Any) -> list[Any]:
    from langmem import create_manage_memory_tool, create_search_memory_tool

    return [
        create_manage_memory_tool(
            ("hub", "learnings"),
            store=store,
            instructions="Store reusable facts, decisions, and learnings about agents and tasks.",
        ),
        create_search_memory_tool(
            ("shared", "docs"),
            store=store,
            instructions="Search shared documentation and architecture notes.",
        ),
    ]


class HubOrchestrator:
    """Stateful orchestrator with per-session thread isolation."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self._model = model
        self._registry = load_registry()
        self._session_id = str(uuid.uuid4())
        logger.info(
            "HubOrchestrator starting with model=%s, agents=%s",
            self._model,
            [spec.id for spec in self._registry],
        )
        logger.debug(
            "Agent specs: %s",
            [f"{spec.id}:{spec.runtime.get('mode','manual')}" for spec in self._registry],
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
            ["hub_learnings", "shared_docs"],
        )
        logger.debug("System prompt length=%d chars", len(_SYSTEM_PROMPT))

        return create_react_agent(
            model=llm,
            tools=tools,
            prompt=_SYSTEM_PROMPT,
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

    def invoke(self, message: str) -> str:
        logger.info("Received user request: %s", message)
        logger.debug("Invoking graph with session_id=%s", self._session_id)
        config = {"configurable": {"thread_id": self._session_id}}
        result = self._graph.invoke(
            {"messages": [HumanMessage(content=message)]},
            config=config,
        )
        messages = result.get("messages", [])
        if not messages:
            raise RuntimeError("Orchestrator returned no messages.")
        return messages[-1].content
