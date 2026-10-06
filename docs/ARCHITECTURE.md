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

## Human MCP external tools

When `HUB_HUMAN_MCP_ENABLED=true`, Hub opens one persistent local stdio MCP
session using `config/human_mcp.json`. The current local transport reuses the
canonical Human MCP implementation through its existing Windows stdio wrapper;
it does not expose the Windows loopback HTTP port to WSL and does not use the
legacy public ngrok path.

The config contains an explicit tool allowlist. Hub currently admits bounded
browser, Google Sheets, Google Docs and read-only Gmail capabilities. Human
MCP filesystem, repository and arbitrary shell tools are deliberately not
exposed to the Hub model by this integration.

Tool safety comes from Human MCP's MCP annotations:

- `readOnlyHint=true`: Hub may execute the tool directly;
- state-changing tools: the LangGraph tool call interrupts and persists the
  task as `waiting_approval`; `/approve` resumes the same graph checkpoint and
  `/reject` resumes with a rejection without executing the external action.

Browser read-only calls have an additional context precondition. Automatic
browser research is disabled unless `config/human_mcp.json` explicitly
configures the session/tab tool schemas, bounded per-task call budget, and
the arguments and response fields that prove an isolated, agent-owned session
and a tab attached to that session. The first eligible read-only browser call
creates that context; later calls reuse it. Direct calls to the session/tab
setup tools remain approval-gated, including when a server annotates them as
read-only. Missing setup tools, schema mismatches, failed setup, or missing
isolation/ownership evidence fail closed without selecting an existing tab.
Each setup and browser call emits a bounded audit event without logging its
arguments or page contents.

An explicit operator request for Human MCP or an allowlisted Human MCP tool is
routed to Hub directly before specialist classification. This prevents browser
or Google Workspace work from being misrouted to AI Tech Lead.

Startup health connects to the configured MCP transport and reports the number
of allowlisted read-only and approval-gated tools. Local deployments fail
closed when Human MCP is enabled but unavailable. Repository/CI default is
disabled, so environments without the local bridge do not attempt to launch it.

Human MCP currently advertises no general Google Drive API tool. Hub therefore
must not claim native Drive listing/search support. Browser automation can
still operate a workflow-owned signed-in Chrome tab when the operator asks for
a browser-based Drive workflow.

## Bounded specialist fan-out

Hub exposes one orchestration-owned `parallel_specialist_fanout` tool for an
operator request that explicitly asks it to coordinate independent specialist
work across projects. The implementation uses LangGraph `Send` branches and a
reducer/join; it does not create a separate orchestration framework.

Fan-out is bounded by `config/fanout.json` (currently at most four branches
and at most three concurrent branches). Every branch must explicitly provide:

- a registered specialist id;
- a task kind advertised by that specialist;
- a bounded task;
- a project reference.

Project references may use an exact known alias or an explicit absolute
directory. The path is canonicalized using the same project-context rules as
`/project`; the specialist still owns its own project authorization and may
reject a root that Hub can resolve.

All branches are validated before child runs are created. Parallel branches
must target distinct canonical project ids. Hub refuses same-project fan-out
instead of guessing that the work is read-only. This preserves the existing
one-active-task-per-project safety rule.

The parent and every branch are persisted as normal task runs. Child context
records the parent id and branch index; the parent records its child ids.
`/tasks` labels the relationship as `fanout-parent:N` and
`child-of:<id>`. Branch success, failure and specialist pause states remain
visible independently. Joined results are deterministic by branch index, and
one branch failure is surfaced rather than retried automatically.

Stopping a fan-out parent requests cancellation for every child and then
cancels the parent. A child may also observe cancellation cooperatively while
the parent stop is executing. The parent invocation checks its persisted
terminal state before delivering a final reply, so a cancelled fan-out cannot
emit a late branch summary after the operator has already stopped it.

## Specialist registry and dispatch

Hub reads staged `agent.json` definitions from Agent Factory. Routing eligibility comes from the specialist's advertised task capabilities in its task contract; purpose is descriptive context only. Runtime, input, interaction and project-context contracts describe how Hub may call the eligible specialist.

Before each incoming turn, Hub performs bounded registry reconciliation. `/agents-refresh` forces an immediate reread; `/agents-status` reports the last reconciled state without rereading.

Every dispatched run pins the selected specialist definition and fingerprint. A paused run resumes against its pinned definition instead of silently adopting a later registry change.

Hub sends one universal task envelope. It adds the classified `task_kind` and resolves project resources only when the specialist's input contract advertises the corresponding envelope field. A persisted backlog resource is the reusable source (provider, spreadsheet ID, sheet name and optional source metadata), not an individual backlog item. When the request contains one explicit item identifier, Hub composes the existing structured `backlog_reference` from that request item and the selected source. Required missing, stale, conflicting, or ambiguous context causes a clear stop; Hub does not guess or silently fall back.

When Hub reformulates an operator request into a shorter specialist task, dispatch also includes
the exact original operator request as clearly labelled verbatim source context. The reformulated
task remains authoritative for scope, and the source block cannot expand permissions or override
the specialist contract. This prevents literal handoff contracts or evidence from being lost during
LLM task reformulation.

A successful specialist result may optionally include the generic `next_task` contract. Hub accepts
only `task_kind` and `task` as required non-empty strings plus an optional list of string
`references`; extra fields such as `agent_id`, lifecycle fields, `project_root`, `context` and
`metadata` are rejected. Hub validates the task kind against the current eligible specialist
registry and preserves a valid result. In Phase 1, Hub then stops: it does not create a child run,
dispatch another specialist, parse Factory prose, or add a human checkpoint. A `next_task` on any
non-success result, or a task kind that is not currently routable, fails closed.

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
