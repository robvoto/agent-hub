# Cross-specialist execution plan — learning view

This diagram explains the implemented Phase 2 Hub-owned transition gate. It is a learning view,
not a claim that every box is a LangGraph node.

![Cross-specialist execution plan](08-CROSS-SPECIALIST-EXECUTION-PLAN.svg)

Source: [`08-CROSS-SPECIALIST-EXECUTION-PLAN.mmd`](08-CROSS-SPECIALIST-EXECUTION-PLAN.mmd) · Rendered: [`08-CROSS-SPECIALIST-EXECUTION-PLAN.svg`](08-CROSS-SPECIALIST-EXECUTION-PLAN.svg)

Learning boundary:

- **LLM reasoning** interprets ambiguous natural language.
- **Contract validation, eligibility filtering, lifecycle state, approvals and stop conditions** stay deterministic where rules are known.
- The Phase 2 entry is a successful-result/test-fixture path with an explicit structured
  `next_task` and approved evidence. Factory Brain runtime emission of those fields is later work.
- `specialist_pending_decision` remains the separate specialist-owned resume path.
