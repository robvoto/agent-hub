# Agent Hub Commands

Operational commands for Agent Hub.

Run all commands from WSL:

```bash
cd ~/projects/agent-hub
```

## Setup

```bash
uv sync
```

Run this again after pulling changes that affect the project name or console scripts so the generated venv entrypoints stay in sync.

## CLI

```bash
uv run agent-hub --help
uv run agent-hub chat
uv run agent-hub --debug chat
```

Equivalent wrapper:

```bash
./run.sh chat
./run.sh chat --debug
```

Interactive chat supports:

- `/agents` to list callable specialists
- `/approve` to resume a paused approval
- `/forget <memory id>` to delete a stored hub learning
- `/help` to show the command list
- `/last` to show the most recently completed or failed task
- `/learn <instruction or fact>` to store an explicit, immediately-authoritative hub
  learning (typed as `semantic`/`operator`/`active` internally — see below). Active
  learnings are folded into the orchestrator's system prompt on every turn (most
  recent first, bounded to keep prompts from growing unbounded). Operator-authored
  `/learn` records are never deleted by compaction; only future automatically
  extracted memory is subject to the 25-record compaction threshold (oldest overflow
  beyond the 15 most recent merged into one `hub-compaction` summary via an LLM call).
- `/learn-mode [on|off]` to toggle automatic background learning (**off by default**).
  When on, after a session goes quiet (5 minutes since the last completed task) Hub
  reviews that session once and may store a high-confidence fact/preference/correction
  as a `scope=auto` semantic memory — never routing, permissions, budgets, or prompts.
  You get a passive one-line Telegram FYI (`\U0001f9e0 Learned: ...`) when it stores
  something; nothing is ever silently applied without that notice. A hub restart loses
  the on/off flag and any pending timer — by design, not a bug.
- `/memory` to list stored hub learnings, showing each record's `type` and `status`
- `/new` to start a fresh LangGraph thread for future turns without cancelling
  active work in the current conversation
- `/project [<path>|clear]` to set/show/clear the target project passed to specialists
- `/reject optional reason` to reject a paused approval
- `/reset` to cancel the current active or paused task and its running specialist
  process tree for this conversation, then immediately start a fresh LangGraph thread
- `/status` to show the current active or paused task, including current phase,
  latest progress summary, last specialist activity, and live-progress state
- `/stop` to cancel the current active or paused task and its running specialist
  process tree while keeping the same conversation/session

### Thread Control Model

- normal messages continue the current LangGraph thread for the active session/project
- if a task is waiting for clarification, the next normal message resumes that same paused run
- `/approve` resumes the same paused run when Hub is waiting on approval
- `/new` starts a fresh empty LangGraph thread; it does not branch the current one
- `/stop` and `/reset` cancel active work; cancelled runs are not resumable
- `/fork` and a generic `/resume` command are not implemented yet; they are tracked as planned work in `AGENT-HUB-030`

### Memory model

Hub memory (`src/agent_hub/hub_memory.py`) is stored in typed namespaces:

- **type**: `semantic` (facts/instructions), `episodic` (past-run lessons), or `procedural`
  (behavior/routing rules) — only `semantic` is populated today; the other two are reserved
  for future automatic extraction.
- **scope**: `operator` (came from your `/learn`) or `auto` (system-derived, e.g. a
  compaction summary). Only `auto` records are ever compacted.
- **status**: `active`, `pending`, `rejected`, or `disabled`. Only `active` records are
  ever injected into the orchestrator's prompt. Everything `/learn` creates is `active`
  immediately — no approval gate.
- if a task is waiting for clarification, the next normal message is treated as the clarification reply

## Telegram gateway

```bash
uv run agent-hub telegram
```

Equivalent wrapper:

```bash
./run.sh telegram
```

Run Telegram with debug logging:

```bash
uv run agent-hub --debug telegram
./run.sh telegram --debug
```

Telegram task runs now expect streamed specialist progress. For a routed
specialist task, Hub should send an immediate acknowledgement, meaningful phase
updates while work is active, quiet heartbeats only after a silent interval, and
the final result without duplicate progress spam.

## Tests

```bash
uv run pytest
```

## Diagram generation

Regenerate the live Agent Hub LangGraph diagram from the current registry:

```bash
python3 scripts/generate_hub_langgraph_diagram.py
```

## Logs

Logs are written to:

```text
logs/agent-hub.log
logs/agent-hub-debug.log
data/llm_usage.json
```

- `logs/agent-hub.log`: human-readable workflow log
- `logs/agent-hub-debug.log`: full technical debug trace

Override the log directory with:

```bash
export HUB_LOG_DIR=/path/to/logs
```

## Environment

Copy `.env.example` to `.env` and set the required values.

```text
OPENAI_API_KEY=...
HUB_BOT_TOKEN=...
HUB_ALLOWED_CHAT_IDS=...
HUB_MODEL=gpt-4.1-mini
```

`HUB_ALLOWED_CHAT_IDS` is a comma-separated list of Telegram chat IDs.

Optional integration variables:

```text
AGENT_FACTORY_ROOT=...
AGENT_FACTORY_KNOWLEDGE_DB=...
HUB_LLM_COST_CATALOG=...
```
