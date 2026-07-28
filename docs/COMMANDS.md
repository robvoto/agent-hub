# Agent Hub operations and commands

Run from WSL:

```bash
cd ~/projects/agent-hub
uv sync
```

## Start

```bash
uv run agent-hub chat
uv run agent-hub telegram
```

Debug mode:

```bash
uv run agent-hub --debug chat
uv run agent-hub --debug telegram
```

The `./run.sh` wrapper accepts the same `chat`, `telegram` and `--debug` choices.

## Operator commands

| Command | Behaviour |
|---|---|
| `/help` | Show current command help |
| `/agents` | List callable specialists |
| `/agents-refresh` | Reread the staged registry and report changes/errors |
| `/agents-status` | Show last reconciled registry state without rereading |
| `/hub-status` | Show Hub session/project/learning/agent summary without starting a new thread |
| `/project [<path>|clear]` | Show, set or clear canonical project context |
| `/status` | Show current active or paused task and latest progress |
| `/last` | Show the most recent terminal task |
| `/new` | Start a fresh thread; do not cancel active work |
| `/stop` | Cancel current work and keep the same conversation |
| `/reset` | Cancel current work and start a fresh conversation |
| `/approve` | Resume an approval pause |
| `/reject [reason]` | Reject and close an approval pause |
| `/learn <lesson>` | Store authoritative memory, analyse it and apply only governed skill actions |
| `/memory` | List stored Hub learnings |
| `/forget <memory-id>` | Remove one stored learning |
| `/learn-mode [on|off]` | Show or change automatic background semantic learning for this session |

A normal message resumes an active clarification or decision pause. For decisions, reply with the option number or name.

Not implemented: `/fork`, generic `/resume`, `/model`, `/med`, `/high`.

## Memory behaviour

- `/new` clears future conversational context by rotating to a new LangGraph thread.
- `/new` does not delete long-term memory or runtime skills.
- `/learn` stores first, then analyses. Analysis failure never discards the stored lesson.
- A valid skill classification may create or version a runtime skill in the knowledge store.
- Documentation, backlog, code and new-agent classifications are recommendations only.
- `/learn-mode` automatic learning is independent of explicit `/learn`.

## Tests

```bash
uv run pytest -q
```

Run the real transport checklist after changes that affect commands, sessions, memory, routing, progress or pause/resume behaviour:

```text
docs/VALIDATION.md
```

## Diagrams

```bash
python3 scripts/generate_hub_langgraph_diagram.py
./scripts/render_hub_mermaid_diagrams.sh
```

Generated Mermaid source and SVG output live under `docs/diagrams/`.

## Logs

```text
logs/agent-hub.log
data/llm_usage.json
```

Debug mode additionally writes:

```text
logs/agent-hub-debug.log
```

Override log location with `HUB_LOG_DIR`.

## Environment

Copy `.env.example` to `.env`. Core values:

```text
OPENAI_API_KEY=...
HUB_BOT_TOKEN=...
HUB_ALLOWED_CHAT_IDS=...
HUB_MODEL=gpt-4.1-mini
```

`HUB_ALLOWED_CHAT_IDS` is comma-separated. Optional integration settings are documented in `.env.example`; treat that file and `src/agent_hub/config.py` as current truth rather than duplicating every variable here.
