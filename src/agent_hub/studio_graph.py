"""LangGraph Studio entrypoint for the real Hub lifecycle.

LangGraph Studio runs graph nodes on an async server event loop. Hub's
production lifecycle is intentionally synchronous because it owns SQLite
persistence and specialist subprocesses. This entrypoint provides only the
async boundary required by Studio and delegates the turn to
``HubOrchestrator`` lifecycle methods; it does not duplicate routing or
dispatch logic.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, MessagesState, StateGraph

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agent_hub.orchestrator import HubOrchestrator  # noqa: E402


class StudioState(MessagesState):
    """Studio messages plus the Hub session selected for this thread."""

    hub_session_id: str


def _studio_session_id(config: RunnableConfig) -> str:
    """Map Studio's persisted thread to an isolated Hub conversation."""
    configurable = config.get("configurable", {})
    thread_id = configurable.get("thread_id") if isinstance(configurable, dict) else None
    if not isinstance(thread_id, str) or not thread_id.strip():
        thread_id = str(uuid.uuid4())
    return f"studio:{thread_id}"


def _latest_user_message(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
        elif isinstance(message, dict) and message.get("role") in {"user", "human"}:
            content = message.get("content")
        else:
            continue
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text_blocks = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            text = "".join(text_blocks).strip()
            if text:
                return text
        if not content:
            raise ValueError("Studio input must contain a non-empty text user message.")
        raise ValueError("Studio input must contain text content.")
    raise ValueError("Studio input must contain a user message.")


async def _invoke_hub(
    state: StudioState, config: RunnableConfig
) -> dict[str, list[AIMessage] | str]:
    """Run one Studio turn through the production Hub orchestration lifecycle."""
    message = _latest_user_message(state.get("messages", []))
    session_id = state.get("hub_session_id") or _studio_session_id(config)
    reply, current_session_id = await asyncio.to_thread(
        _invoke_hub_sync, message, session_id
    )
    return {
        "messages": [AIMessage(content=reply)],
        "hub_session_id": current_session_id,
    }


def _invoke_hub_sync(message: str, session_id: str) -> tuple[str, str]:
    """Keep all synchronous Hub setup and execution off Studio's event loop."""
    orchestrator = HubOrchestrator(session_id=session_id)
    return _process_operator_turn(orchestrator, message), orchestrator.session_id


def _process_operator_turn(orchestrator: HubOrchestrator, message: str) -> str:
    """Apply the CLI/Telegram lifecycle contract to one Studio message.

    Studio has no separate command transport, so its slash commands arrive as
    ordinary graph input. They must be handled by the same Hub lifecycle
    methods as CLI/Telegram instead of being passed to the model as a new
    task. Plain text follows the existing gateway order: resume a decision or
    clarification when one is pending, otherwise start a fresh invocation.
    """
    text = message.strip()
    if text == "/approve":
        return orchestrator.approve_pending()
    if text == "/reject" or text.startswith("/reject "):
        return orchestrator.reject_pending(text[len("/reject") :].strip() or "Rejected by user")
    if text == "/stop" or text.startswith("/stop "):
        return orchestrator.stop_current_task(identifier=text[len("/stop") :].strip() or None)

    command = text.split(maxsplit=1)[0] if text.startswith("/") else ""
    if command == "/status" and text == command:
        return orchestrator.current_run_status()
    if command == "/tasks" and text == command:
        return orchestrator.tasks_status()
    if command == "/hub-status" and text == command:
        return orchestrator.hub_status()
    if command == "/last" and text == command:
        return orchestrator.last_run_status()
    if command == "/new" and text == command:
        return orchestrator.new_session()
    if command == "/reset" and text == command:
        return orchestrator.reset_session()
    if command == "/reset-all" and text == command:
        return orchestrator.reset_all()
    if command == "/resume":
        identifier = text[len("/resume") :].strip()
        return "Usage: /resume <id>" if not identifier else orchestrator.resume_task(identifier)
    if command == "/help" and text == command:
        return (
            "Studio lifecycle commands: /status, /tasks, /approve, /reject [reason], "
            "/stop [id], /resume <id>, /new, /reset, /reset-all."
        )
    if text.startswith("/"):
        return (
            "Unsupported Studio command. Use /status, /tasks, /approve, /reject, "
            "/stop, or /help."
        )

    pending = orchestrator.pending_run()
    if pending is not None and pending.state == "waiting_decision":
        return orchestrator.provide_decision_reply(text)
    if pending is not None and pending.state == "waiting_clarification":
        return orchestrator.provide_clarification(text)
    return orchestrator.invoke(text)


def _build_graph() -> Any:
    builder = StateGraph(StudioState)
    builder.add_node("hub", _invoke_hub)
    builder.add_edge(START, "hub")
    builder.add_edge("hub", END)
    return builder.compile()


graph = _build_graph()
