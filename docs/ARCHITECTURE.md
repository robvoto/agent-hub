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
| `input_contract` | no | declares the `agent-hub.task` protocol version, required/optional envelope fields, and which context fields (`project_root`, `references`) the specialist reads (`accepted_context`) or cannot function without (`required_context`) — Hub reads these generically to decide what a dispatch actually sends (see below) |
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
resume/approval fields when resuming a paused run. The envelope-building code
does not vary per specialist.

What context actually reaches a given specialist does vary, generically, by
its own `input_contract` declaration: `_resolve_dispatch_context` in
orchestrator.py narrows `project_root`/`references` to whatever the
specialist's `accepted_context` names (a specialist with no declaration is
treated as accepting both, preserving the default every specialist had before
this declaration existed). If `required_context` names something the
dispatch doesn't have — no project selected via `/project`, most commonly —
Hub fails the task immediately with a clear message instead of sending an
incomplete envelope and letting the specialist guess or fail downstream.

### Canonical project context (AGENT-HUB-039)

`/project <path>` no longer just remembers a filesystem path. Hub resolves it
to a canonical `ProjectContext` (`project_context.py`): a `project_id`
(derived from the project's git remote when one exists, so it stays stable
across clones and local path layout; falls back to the resolved absolute path
otherwise — Hub does not maintain a separate project registry, identity is
derived, not assigned), a `contract_version` for this schema, a `fingerprint`
hashed over that identity, and light metadata (`name`, `vcs`, `remote`). That
full context, not a raw path, is what's persisted per session.

Before every *fresh* dispatch that would send a specialist `project_root`,
`ProjectContextRegistry.resolve_for_dispatch` recomputes the context from the
persisted root and compares it to what was stored: if the root no longer
exists, or the recomputed identity/fingerprint no longer matches (the root
now resolves to a different project than when it was selected), the dispatch
stops immediately with a clear failed status instead of silently sending a
stale path — but only for a specialist whose `accepted_context` actually
includes `project_root`; an unrelated specialist isn't blocked by someone
else's stale selection. `project_id`, `project_contract_version`, and
`project_fingerprint` ride alongside `project_root` in the envelope whenever
it's sent, as additive flat fields a specialist that doesn't recognize them
simply ignores (see `docs/agent-contract.md`'s "no fallback" note: Agent
Factory and ai-tech-lead have not adopted this richer vocabulary yet — Hub
sends it, but "stop on unknown project" is enforced Hub-side, not by
specialist-side parsing).

A *resumed* dispatch — clarification, decision, or approval resume, all
three — never re-resolves `/project`. Each replays the exact `ProjectContext`
pinned at the original dispatch (stored in the task's context as
`agent_dispatch_project_*` fields, alongside the original `references`), so
a selection change or staleness introduced while a task was paused can't
retroactively change what a resume sends.

### Registry reconciliation & manifest pinning (AGENT-HUB-040)

`HubOrchestrator._reconcile_registry` re-reads Factory's registry before
every turn (bounded to once per incoming message) and rebuilds the callable
tool set only when something actually changed (added/changed/removed by spec
equality) — a no-op turn does no extra work. An operator can also force this
immediately with `/agents-refresh`, which reports what changed plus current
registry health: `registry.load_registry_report` surfaces any `agent.json`
that failed to parse as a `RegistryLoadError` (source + message) instead of
silently dropping it, so an invalid manifest is visible instead of an agent
just quietly not showing up. `/agents-status` is the read-only counterpart —
it reports each active agent's id/version/fingerprint and any invalid
manifest as of the last reconciliation, without itself re-reading anything,
so it's safe to poll.

Every dispatch pins the exact `AgentSpec` it used — the full manifest plus
`registry.spec_fingerprint`, a content hash — into the task run's persisted
context (`pinned_agent_spec`/`pinned_agent_version`/`pinned_agent_fingerprint`).
`HubOrchestrator._require_spec` prefers that pinned snapshot over the live
registry when resuming a paused task: if Factory changed or removed the
agent while the task was paused, resume still runs against the manifest
version the task was actually dispatched against, rather than silently
picking up different behavior or failing just because the id moved. Paused
tasks created before this pinning existed have no pinned snapshot and fall
back to a live-registry lookup by id, as before.

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
