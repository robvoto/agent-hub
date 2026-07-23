# Hub Routing — Generic Specialist Path

This page now shows only the default routed-task path.
Each other relevant path has its own diagram so the interaction stays readable.

- Clarification loop: [05B-HUB-CLARIFICATION.md](05B-HUB-CLARIFICATION.md)
- Factory approval flow: [05C-HUB-FACTORY-APPROVAL.md](05C-HUB-FACTORY-APPROVAL.md)
- Explicit `/learn` command: [05D-HUB-EXPLICIT-LEARNING.md](05D-HUB-EXPLICIT-LEARNING.md)

![Hub routing generic path](05-HUB-ROUTING.svg)

Source: [05-HUB-ROUTING.mmd](05-HUB-ROUTING.mmd) | Rendered asset: [05-HUB-ROUTING.svg](05-HUB-ROUTING.svg)

## Why Preferences Appear Here

Hub checks already-saved preferences before routing. That can affect specialist
selection or how the task is framed, but it does not mean Hub is creating new
learning from the first message.

## What “Result Or Next Action” Means

That label is intentionally broader than the old “summary + instruction” text.

- On success, the specialist may return a plain result summary, an execution instruction, or both.
- On other paths, Hub may send back a clarification question or an approval prompt instead.

The operator-visible message is formatted from the specialist status contract in
`src/agent_hub/orchestrator.py` `_format_output()`.

## What This Diagram Does Not Show

This diagram stays simplified on purpose.

- Hub does read saved preferences before routing.
- Hub does stream its own internal LangGraph node/task events.
- Hub now also expects streamed progress events from subprocess specialists while
  they are running.

What is omitted here is the progress detail itself: the specialist can send
phase updates and heartbeats back to Hub during execution, and Hub can surface
those live in Telegram and `/status`.

## Implementation Map

- Router prompt rebuild: `src/agent_hub/orchestrator.py` `_build_system_prompt`
- Preference formatting: `src/agent_hub/hub_memory.py` `format_learnings_for_prompt`
- Explicit memory writes: `src/agent_hub/orchestrator.py` `learn`

Examples of selected specialists live in separate path diagrams, such as Factory
approval and explicit learning.
