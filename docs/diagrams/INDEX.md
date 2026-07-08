# Agent Hub — Diagrams

Orchestration workflow diagrams. All diagrams describe hub-level flows.

| File | What it shows |
|------|---------------|
| [05-HUB-ROUTING.md](05-HUB-ROUTING.md) | How hub receives a message, selects an agent, dispatches, and returns the result |
| [06-CODING-TASK-END-TO-END.md](06-CODING-TASK-END-TO-END.md) | Full flow from Telegram message through hub → ai-tech-lead → coding agent → result |

## How to read these

- **Entry point:** `@agent_hub_vot_bot` on Telegram, or `bash run.sh chat`
- **Run commands:** documented in the root `README.md`
- **Orchestrator:** `src/agent_hub/orchestrator.py` — `HubOrchestrator._dispatch_subprocess()`
- **Registry:** `agent-factory/config/agents/` — where enabled agents are declared
- **Dispatch:** subprocess call to agent's `runtime.entrypoint` with `--input-json` / `--output-json`

## Render diagrams

Diagrams are Mermaid (`.mmd`). Render with:

```bash
# In agent-factory (has the render script):
cd ~/projects/agent-factory && bash docs/diagrams/render.sh
```
