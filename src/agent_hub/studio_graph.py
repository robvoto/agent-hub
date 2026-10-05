"""LangGraph Studio adapter for the real Hub specialist workflow.

This is development/visualisation wiring only. It uses Hub's real registered
specialist tools, model configuration, and system prompt, but leaves persistence
to LangGraph Studio as required by the LangGraph API. Hub-only support tools
that start external gateways are excluded so merely opening Studio has no
external side effects.

The production and Studio graphs therefore share the same ReAct topology:
__start__ -> agent <-> tools -> __end__.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from agent_hub.config import chat_model_kwargs, configured_model  # noqa: E402
from agent_hub.orchestrator import (  # noqa: E402
    _build_system_prompt,
    _load_specialists,
    _make_agent_tool,
)

registry = _load_specialists()
model = configured_model()
tools = [_make_agent_tool(spec, result_registry=registry) for spec in registry]

graph = create_react_agent(
    model=ChatOpenAI(**chat_model_kwargs(model)),
    tools=tools,
    prompt=_build_system_prompt,
)
