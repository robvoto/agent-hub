# Agent Hub — Architecture

## Responsibility

Agent Hub is the **main orchestrator, runtime, and control plane** for the agent platform.

- Receives all user input (Telegram / CLI)
- Maintains session state and conversation history
- Routes tasks to the correct specialist agent
- Tracks each task run through explicit lifecycle states
- Supports explicit approval resume and clarification resume for paused work
- Owns the hub knowledge store
- Reports results back to the user

Agent Hub does **not** create, configure, or stage agents. That is `agent-factory`'s job.

## Repo split

```
agent-hub        ← this repo
  orchestrator   — LangGraph supervisor graph
  registry       — reads agent specs from agent-factory
  telegram       — user-facing Telegram bot
  cli            — chat and telegram commands
  task_runs      — persisted task lifecycle and run events
  manifest_cache — cached specialist handshakes by manifest hash
  cost_log       — hub LLM usage and cost logging
  knowledge_store — hub runtime knowledge (data/)
  checkpointer   — LangGraph SQLite checkpoint store

agent-factory    ← specialist agent
  factory_brain  — designs and stages agent packages
  telegram       — factory admin bot (staging, approvals)
  storage        — staged_agents, approvals, factory_threads
  agent_spec     — Pydantic validation for agent packages
  agent_catalog  — staged + enabled agent inventory
  creator_workflow — deterministic scaffolding workflow

ai-tech-lead     ← specialist agent
  (separate repo, called by hub)
```

## Data flow

```
User (Telegram / CLI)
  │
  ▼
Hub Telegram Gateway / CLI
  │
  ▼
HubOrchestrator (LangGraph)
  │
  ├── reads registry from agent-factory/config/agents/
  │
  ├── dispatches to specialist agents
  │   ├── ai-tech-lead  (coding tasks)
  │   ├── agent-factory (create/configure agents via factory bridge)
  │   └── future specialists
  │
  ├── caches specialist manifests in data/agent_manifest_cache.json
  │   └── refreshes by manifest hash / TTL instead of rereading docs every turn
  │
  ├── records task lifecycle in data/task_runs.sqlite3
  │   └── states: received → routed → dispatched → in_progress → waiting_* / succeeded / failed
  │
  ├── records hub LLM usage in data/llm_usage.json
  │   └── token counts are always logged; cost stays unknown unless the catalog has a verified rate
  │
  └── searches shared docs across hub and factory knowledge stores
```

## Agent registry contract

Agents are registered in `agent-factory/config/agents/<id>/agent.json`. Hub reads:

| Field | Required | Purpose |
|-------|----------|---------|
| `id` | yes | unique identifier |
| `name` | yes | display name |
| `purpose` | yes | used by orchestrator for routing decisions |
| `aliases` | yes | command aliases |
| `tools` | yes | declared tool list |
| `version` | no | defaults to "1.0.0" |
| `backlog_sheet_id` | no | Google Sheets spreadsheet ID for this agent's backlog — hub uses this to add backlog items without hardcoded URLs |

Factory is responsible for writing all fields. Hub reads but never writes.

### Backlog routing

When the user asks hub to add a backlog item for a specialist agent, hub:
1. Looks up the agent in the registry by name/alias
2. Reads `backlog_sheet_id` from the agent's spec
3. Appends the item to that agent's Google Sheet
4. If `backlog_sheet_id` is null, reports that the agent has no backlog sheet configured

This means hub can manage any agent's backlog without hardcoding sheet locations — the location is declared in the agent's own registry entry.

## Persistence

| Store | Path | Owner |
|-------|------|-------|
| Task runs | `data/task_runs.sqlite3` | Hub |
| Manifest cache | `data/agent_manifest_cache.json` | Hub |
| LLM usage log | `data/llm_usage.json` | Hub |
| Knowledge store | `data/knowledge_store.sqlite3` | Hub |
| Checkpoints | `data/checkpoints.sqlite3` | Hub |
| Staged agents | `factory/data/agent_factory.sqlite3` | Factory |
| Factory checkpoints | `factory/data/factory_checkpoints.sqlite3` | Factory |
| Factory knowledge | `factory/data/knowledge_store.sqlite3` | Factory |

## Backlog

Live backlog: https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178

This is the one true backlog sheet for this project. Hub agents may write to and use it as needed; no other backlog sheet is used.
