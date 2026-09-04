# Agent Hub architecture

## Purpose and ownership

Agent Hub is the operator-facing runtime orchestrator. It:

- receives CLI and Telegram input;
- maintains sessions, LangGraph threads and task lifecycle;
- reconciles the staged specialist registry;
- routes bounded work to specialists;
- handles clarification, decision and approval pauses;
- persists progress, results, cost and Hub-owned knowledge;
- returns concise operator-facing status and results.

Agent Factory owns specialist creation, manifests, validation and staging. Specialists own their internal implementation workflows and repository-local state.

## Runtime flow

```text
Operator request
    -> load canonical project context + bounded relevant memory
    -> load specialist manifests
    -> classify advertised task kind
    -> deterministically filter eligible specialists
    -> resolve request project against known canonical project metadata
    -> resolve required project resources
    -> build bounded task envelope
    -> dispatch selected specialist
    -> stream progress / handle pause-resume
    -> persist result
    -> operator response
    -> optional governed learning/resource update
```

Hub has bounded tools. It does not gain arbitrary filesystem, code, backlog or Agent Factory write access merely because an LLM recommends an action.

## Specialist registry and dispatch

Hub reads staged `agent.json` definitions from Agent Factory. Routing eligibility comes from the specialist's advertised task capabilities in its task contract; purpose is descriptive context only. Runtime, input, interaction and project-context contracts describe how Hub may call the eligible specialist.

Before each incoming turn, Hub performs bounded registry reconciliation. `/agents-refresh` forces an immediate reread; `/agents-status` reports the last reconciled state without rereading.

Every dispatched run pins the selected specialist definition and fingerprint. A paused run resumes against its pinned definition instead of silently adopting a later registry change.

Hub sends one universal task envelope. It adds the classified `task_kind` and resolves project resources only when the specialist's input contract advertises the corresponding envelope field. A persisted backlog resource is the reusable source (provider, spreadsheet ID, sheet name and optional source metadata), not an individual backlog item. When the request contains one explicit item identifier, Hub composes the existing structured `backlog_reference` from that request item and the selected source. Required missing, stale, conflicting, or ambiguous context causes a clear stop; Hub does not guess or silently fall back.

## Project context

`/project <path>` resolves a canonical project context containing the root, stable project identity, contract version and fingerprint. A request may explicitly name one already-known project alias; otherwise a valid current `/project` selection is used. Unknown or ambiguous project references stop safely. Fresh dispatches revalidate the resolved context. Paused runs replay the project context pinned at original dispatch, even if the operator changes `/project` before resuming.

Project-owned resources are persisted separately from the selected session context. Each resource stores the canonical `project_id`, a resource type, structured location, source, timestamps and opaque metadata. The generic registry currently has a backlog convenience type and upserts an existing resource when project, type and location match; it does not create another project identity system.

## Task lifecycle and interaction

Task runs are stored in `data/task_runs.sqlite3`. The runtime records received, routed, dispatched, active, paused and terminal states.

Supported pause/resume paths include:

- clarification: the next normal operator message resumes the same task;
- approval: `/approve` or `/reject`;
- decision: reply normally with the option number or name.

`/stop` cancels the current task while retaining the conversation. `/reset` cancels it and starts a new conversation. Cancelled work is not resumable.

Subprocess specialists emit bounded progress events. Hub persists current phase, latest meaningful summary and last specialist activity. Heartbeats are quiet and do not fabricate progress.

## Memory and context model

### Short-term memory

Short-term memory is the current LangGraph thread: conversation messages, active task state and pause/resume context. It is persisted through the SQLite checkpointer so a process restart does not automatically erase the thread.

`/new` starts a new thread for future turns. It does not erase long-term memory, runtime skills or historical task records.

### Long-term memory

Long-term Hub learnings are stored in `data/knowledge_store.sqlite3` through `hub_memory.py`.

Records have:

- type: semantic, procedural or episodic;
- scope: operator or automatic;
- status: active, pending, rejected or disabled.

Explicit `/learn` input is stored immediately and remains authoritative. Active operator records rank ahead of automatic records when relevant context is injected. `/memory` lists stored records and `/forget <id>` removes one.

Automatic background semantic learning is controlled per session by `/learn-mode`. It is separate from explicit `/learn` and is off unless enabled.

### `/learn` workflow

`/learn <lesson>` performs this bounded sequence:

1. store the lesson immediately as authoritative long-term memory;
2. retrieve bounded relevant memories, runtime skills and approved documentation;
3. run one structured LLM analysis;
4. classify the memory type and recommended action;
5. optionally validate a high-confidence structured project-resource proposal against the current canonical project and upsert it with memory provenance;
6. create or version a governed Hub runtime skill only when the analysis returns a valid skill action;
7. return documentation, backlog, code-change or new-specialist outcomes as proposals only.

If analysis fails, memory storage still succeeds and the failure is reported plainly.

### Runtime skills versus project instructions

Hub runtime skills are versioned records in the Hub knowledge store, managed by `HubSkillStore`. They are not files under `.agents/skills/`.

The repository `.agents/skills/` directory contains instructions for coding agents working on this codebase. Hub runtime does not read or rewrite those files as its operational skill store.

## Authoritative documentation context

`HubContextService` exposes a small allowlist of current documentation and live registry metadata to `/learn`. Retrieval is read-only and bounded. It does not provide general filesystem search or write capability.

## Persistence

| Data | Path |
|---|---|
| LangGraph short-term thread checkpoints | `data/checkpoints.sqlite3` |
| Task lifecycle and progress | `data/task_runs.sqlite3` |
| Hub long-term memory and runtime skills | `data/knowledge_store.sqlite3` |
| Hub LLM usage and estimated cost | `data/llm_usage.json` |
| Specialist manifest cache | `data/agent_manifest_cache.json` |
| Human-readable runtime log | `logs/agent-hub.log` |
| Technical debug log | `logs/agent-hub-debug.log` |

## Current boundaries and unfinished work

- Model profiles and `/model`, `/med`, `/high` remain backlog work until implemented and verified.
- Documentation, backlog, code-change and new-specialist classifications from `/learn` remain proposals until a separately governed execution path exists.
- Agent Factory dispatch remains dependent on the Factory runtime being callable and healthy.
- Generic `/fork` and `/resume` commands are not implemented.
