# Documentation Index

Start here for Agent Hub documentation.

Read only the smallest document needed for the task.

## Core

- `../README.md` - short project landing page only
- `ARCHITECTURE.md` - runtime/control-plane architecture and repo responsibility split
- `COMMANDS.md` - setup, CLI, Telegram, tests, logs, and environment commands
- `TELEGRAM_MVP_VALIDATION.md` - real Telegram MVP proof runbook and evidence checklist

## Diagrams

- `diagrams/INDEX.md` - diagram index
- `diagrams/05-HUB-ROUTING.md` - hub routing flow
- `diagrams/06-CODING-TASK-END-TO-END.md` - coding task end-to-end flow
- `diagrams/07-HUB-LANGGRAPH-TOOLS.mmd` / `.svg` - generated LangGraph/tool wiring, if present
## Runtime code map

- `../src/agent_hub/orchestrator.py` - LangGraph orchestrator
- `../src/agent_hub/registry.py` - reads staged agent registry from Agent Factory
- `../src/agent_hub/telegram_gateway.py` - Telegram polling gateway
- `../src/agent_hub/cli.py` - CLI entry point
- `../src/agent_hub/knowledge_store.py` - runtime knowledge store
- `../src/agent_hub/checkpointer.py` - runtime checkpoint persistence
- `../src/agent_hub/log_config.py` - logging configuration

## Tests

- `../tests/` - test suite

## Backlog

The live backlog source of truth is the Google Sheet linked from `../README.md`.

Do not create local backlog files unless explicitly requested.
