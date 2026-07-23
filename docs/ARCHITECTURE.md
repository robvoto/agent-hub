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
  └── searches hub's own knowledge store plus any specialist that opts in via knowledge_db
```

## Agent registry contract

Agents are registered in `agent-factory/config/agents/<id>/agent.json`. Hub's
loader (`registry.parse_agent_spec`) reads a small, fixed core:

| Field | Required | Purpose |
|-------|----------|---------|
| `id` | yes | unique identifier |
| `name` | yes | display name |
| `purpose` | yes | the complete routing contract — orchestrator selects a specialist by this field alone. Aliases, project names, and identifier prefixes play no part in routing |
| `tools` | yes | declared tool list |
| `version` | no | the specialist's own version, defaults to "1.0.0" |
| `input_contract` | no | declares the `agent-hub.task` protocol version, required/optional envelope fields, and which context fields (`project_root`, `references`) the specialist reads |
| `interaction_contract` | no | declared lifecycle support — `progress`, `clarification`, `approval`, `resume`, `cancellation`. Hub does not branch dispatch behavior on this; the generic `output_contract`/`runtime.progress` mechanism still drives actual behavior |
| `runtime` | yes | how Hub invokes the agent (subprocess entrypoint, or Agent Factory's in-process `factory_brain` mode) |

Every other top-level `agent.json` field — `backlog_sheet_id`, `knowledge_db`,
`aliases`, `permissions`, `memory`, `output_contract`, or anything a future
specialist invents — is captured verbatim into `AgentSpec.extensions` with no
dedicated field and no Hub code change. `shared_docs.py` reads
`extensions.get("knowledge_db")` this way: if a specialist declares it, Hub's
`search_shared_docs` tool includes that specialist's store, labeled by its
`id`; if not, it isn't searched. Agent Factory's own spec is built in-code by
`factory_bridge.py` rather than read from a JSON file, but sets the same
`extensions` field.

Factory is responsible for writing all fields. Hub reads but never writes.

`search_shared_docs` is not called automatically — it is one tool among several
that the LangGraph react agent may choose to call, and the current system
prompt is focused on dispatching to a specialist rather than searching docs
first, so in practice it is called at the model's discretion, not on every turn.

### Dispatch envelope

Hub dispatches every subprocess specialist through the same universal task
envelope (`task_envelope.build_task_envelope`): task text, request/run
identity, source, execution mode, the selected project when known, any
user-provided or Hub-observed `references` (relayed uninterpreted), and
resume/approval fields when resuming a paused run. The envelope shape does not
vary per specialist — a specialist that doesn't use a field simply ignores it.

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

Hub's knowledge store is shared across Hub sessions and Hub-owned memory/tools.
That is appropriate for operator-established Hub facts such as routing or
interaction preferences. It is not a universal database for every specialist:
specialists keep their own repo-local state unless they are explicitly designed
to read a Hub-owned shared namespace.

## Current Visibility Model

Hub currently has three different kinds of runtime visibility, and they are not
the same thing:

- Telegram ingress is polled by the Hub gateway.
- Hub's own LangGraph run can stream internal task/node events while the router is working.
- Ordinary subprocess specialists now use a two-part contract: Hub sends one
  input JSON that includes a `progress_jsonl` path, specialists append bounded
  progress events to that JSONL stream while they work, and they still write one
  final output JSON at completion.

That means Hub can show live specialist phase updates, heartbeats, and the last
human-readable progress summary while a subprocess specialist is running.
Progress is persisted in the existing `task_runs.sqlite3` database, not a second
runtime database.

`/status` is still a persisted snapshot, but that snapshot now includes live
specialist progress fields such as current phase, latest human summary, and last
specialist activity time. Hub requires streamed specialist progress for
subprocess specialists; a specialist that finishes without emitting progress is
treated as a contract failure, not silently downgraded.

## Backlog

Live backlog: https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178

This is the one true backlog sheet for this project. Hub agents may write to and use it as needed; no other backlog sheet is used.
Hub caches specialist manifests by agent id and manifest hash. If a specialist
manifest declares `manifest_cache_ttl_seconds`, Hub should use that as the
refresh TTL for the full manifest fetch; otherwise it falls back to Hub's default.
