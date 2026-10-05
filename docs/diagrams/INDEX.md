# Agent Hub diagrams

Diagrams are generated views of current runtime behaviour. Code and tests remain authoritative.

| Diagram | Purpose |
|---|---|
| [`01-HUB-ROUTING.md`](01-HUB-ROUTING.md) | End-to-end operator routing and specialist dispatch |
| [`02-HUB-CLARIFICATION.md`](02-HUB-CLARIFICATION.md) | Clarification pause and same-run resume |
| [`03-HUB-FACTORY-APPROVAL.md`](03-HUB-FACTORY-APPROVAL.md) | Factory approval path where the Factory runtime is available |
| [`04-HUB-EXPLICIT-LEARNING.md`](04-HUB-EXPLICIT-LEARNING.md) | Explicit `/learn` storage, analysis and governed action |
| [`05-HUB-LANGGRAPH-TOOLS.md`](05-HUB-LANGGRAPH-TOOLS.md) | Current specialist/tool graph |
| [`06-HUB-LANGGRAPH-NODE-FLOW.md`](06-HUB-LANGGRAPH-NODE-FLOW.md) | Current Hub routing + LangGraph execution learning view |
| [`07-HUB-COMPILED-LANGGRAPH.md`](07-HUB-COMPILED-LANGGRAPH.md) | Low-level compiled topology (`agent` / `tools`); verification only, not the full workflow |
| [`08-CROSS-SPECIALIST-EXECUTION-PLAN.md`](08-CROSS-SPECIALIST-EXECUTION-PLAN.md) | Frozen Factory -> Hub -> human -> implementation plan, shown as a learning process |

Each diagram has matching `.mmd` source and `.svg` output. Regenerate after runtime-flow changes:

```bash
python3 scripts/generate_hub_langgraph_diagram.py
./scripts/render_hub_mermaid_diagrams.sh
```

Do not hand-edit generated SVG files.
