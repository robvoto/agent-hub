# Agent Hub diagrams

Diagrams are generated views of current runtime behaviour. Code and tests remain authoritative.

| Diagram | Purpose |
|---|---|
| [`05-HUB-ROUTING.md`](05-HUB-ROUTING.md) | End-to-end operator routing and specialist dispatch |
| [`05B-HUB-CLARIFICATION.md`](05B-HUB-CLARIFICATION.md) | Clarification pause and same-run resume |
| [`05C-HUB-FACTORY-APPROVAL.md`](05C-HUB-FACTORY-APPROVAL.md) | Factory approval path where the Factory runtime is available |
| [`05D-HUB-EXPLICIT-LEARNING.md`](05D-HUB-EXPLICIT-LEARNING.md) | Explicit `/learn` storage, analysis and governed action |
| [`07-HUB-LANGGRAPH-TOOLS.md`](07-HUB-LANGGRAPH-TOOLS.md) | Current specialist/tool graph |
| [`07B-HUB-LANGGRAPH-NODE-FLOW.md`](07B-HUB-LANGGRAPH-NODE-FLOW.md) | Current Hub routing + LangGraph execution learning view |
| [`08-HUB-COMPILED-LANGGRAPH.svg`](08-HUB-COMPILED-LANGGRAPH.svg) | Low-level compiled topology (`agent` / `tools`); verification only, not the full workflow |
| [`09-CROSS-SPECIALIST-EXECUTION-PLAN.md`](09-CROSS-SPECIALIST-EXECUTION-PLAN.md) | Frozen Factory -> Hub -> human -> implementation plan, shown as a learning process |

Each diagram has matching `.mmd` source and `.svg` output. Regenerate after runtime-flow changes:

```bash
python3 scripts/generate_hub_langgraph_diagram.py
./scripts/render_hub_mermaid_diagrams.sh
```

Do not hand-edit generated SVG files.
