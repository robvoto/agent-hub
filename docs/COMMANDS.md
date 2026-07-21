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
```

Equivalent wrapper:

```bash
./run.sh chat
```

Interactive chat supports:

- `/help` to show the command list
- `/new` to start a fresh session
- `/agents` to list callable specialists
- `/status` to show the current active or paused task
- `/last` to show the most recently completed or failed task
- `/learn <instruction or fact>` to store an explicit hub learning. Stored learnings
  are folded into the orchestrator's system prompt on every turn (most recent
  first, bounded to keep prompts from growing unbounded). Once the raw learning
  count passes 25, the oldest overflow beyond the 15 most recent is automatically
  compacted into one summary learning (source `hub-compaction`) via an LLM call —
  nothing is silently dropped from view, it's merged instead.
- `/memory` to list stored hub learnings
- `/forget <memory id>` to delete a stored hub learning
- `/stop` to cancel the current active or paused task
- `/approve` to resume a paused approval
- `/reject optional reason` to reject a paused approval
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
./run.sh telegram --debug
```

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
data/llm_usage.json
```

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
