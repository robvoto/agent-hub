# Agent Hub — Diagrams

Orchestration workflow diagrams. All diagrams describe hub-level flows.

| File | What it shows |
|------|---------------|
| [05-HUB-ROUTING.md](05-HUB-ROUTING.md) | Generic routed task path to a selected specialist |
| [05B-HUB-CLARIFICATION.md](05B-HUB-CLARIFICATION.md) | Generic clarification pause and resume path |
| [05C-HUB-FACTORY-APPROVAL.md](05C-HUB-FACTORY-APPROVAL.md) | Factory design request with approval resume |
| [05D-HUB-EXPLICIT-LEARNING.md](05D-HUB-EXPLICIT-LEARNING.md) | Explicit `/learn` command path |
| [07-HUB-LANGGRAPH-TOOLS.md](07-HUB-LANGGRAPH-TOOLS.md) | Hub's LangGraph tool wiring (shared-docs search, specialist dispatch) |
| [07B-HUB-LANGGRAPH-NODE-FLOW.md](07B-HUB-LANGGRAPH-NODE-FLOW.md) | Hub's internal `agent -> tools -> agent` node loop as a flowchart |

## How to read these

- **Entry point:** `@agent_hub_vot_bot` on Telegram, or `bash run.sh chat`
- **Run commands:** documented in the root `README.md`
- **Orchestrator:** `src/agent_hub/orchestrator.py` — `HubOrchestrator._dispatch_subprocess()`
- **Registry:** `agent-factory/config/agents/` — where enabled agents are declared
- **Dispatch:** subprocess call to agent's `runtime.entrypoint` with `--input-json` / `--output-json`, plus a `progress_jsonl` path inside the input payload for live progress events

## Render diagrams

Each diagram uses three files on purpose:

- `.mmd` is the Mermaid source we edit.
- `.svg` is the checked-in rendered artifact for places that do not render Mermaid.
- `.md` is the short human wrapper page that explains what the diagram means and links the related paths.

Render the SVGs from the Mermaid source with:

```bash
bash scripts/render_hub_mermaid_diagrams.sh
```

Keep interaction diagrams path-specific, keep note text short, and rerender the
matching SVGs after every Mermaid edit so the checked-in artifacts stay in sync.

**Flowchart exception:** the shared renderer above uses a hand-rolled JSDOM
shim and cannot lay out Mermaid `flowchart` diagrams correctly. Render the
flowchart artifacts in this repo with:

```bash
bash scripts/render_hub_flowcharts.sh
```

That script regenerates:

- `07-HUB-LANGGRAPH-TOOLS.*`
- `07B-HUB-LANGGRAPH-NODE-FLOW.svg`
