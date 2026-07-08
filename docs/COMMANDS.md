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
