# Agent Hub Documentation

This is the canonical technical documentation entry point for Agent Hub. It is intended for developers, reviewers and coding agents; read only the smallest document needed for the task.

| Need | Read |
|---|---|
| Understand ownership, runtime flow, persistence or memory | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| Run the Hub or use commands | [`COMMANDS.md`](COMMANDS.md) |
| Prove a change works through real CLI/Telegram | [`VALIDATION.md`](VALIDATION.md) |
| Inspect workflow diagrams | [`diagrams/INDEX.md`](diagrams/INDEX.md) |
| Review security boundaries | [`../SECURITY.md`](../SECURITY.md) |
| Repository-wide agent rules | [`../AGENTS.md`](../AGENTS.md) |

## Current code map

- `src/agent_hub/orchestrator.py` — LangGraph orchestration and command services
- `src/agent_hub/telegram_gateway.py` — Telegram transport
- `src/agent_hub/cli.py` — CLI transport
- `src/agent_hub/registry.py` — specialist registry loading and reconciliation
- `src/agent_hub/task_runs.py` — persisted task lifecycle
- `src/agent_hub/checkpointer.py` — short-term thread persistence
- `src/agent_hub/hub_memory.py` — long-term learning records and `/learn` analysis
- `src/agent_hub/hub_skills.py` — governed versioned runtime skills
- `src/agent_hub/hub_context.py` — bounded authoritative documentation context
- `src/agent_hub/project_context.py` — canonical selected-project identity
- `src/agent_hub/project_resources.py` — persistent typed resources associated with canonical projects

## Source-of-truth rules

- Runtime behaviour: current code and tests
- Specialist definitions: Agent Factory staged registry
- Delivery work: live Google Sheet backlog linked from `README.md`
- Documentation: this index and its linked files

Keep active documentation current and concise. Replace stale instructions rather than preserving duplicate historical guidance in the active documentation tree.
