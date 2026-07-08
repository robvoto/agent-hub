# AGENTS.md

Purpose: minimal always-loaded repository instructions for AI agents working in Agent Hub.

This file is a routing layer only. It is not the project manual, architecture guide, backlog, standards document, or test plan.

## Project role

Agent Hub is the runtime orchestrator and control plane.

- Hub receives operator input and routes work.
- Hub reads staged agent definitions from Agent Factory.
- Hub dispatches work to specialist agents such as AI Tech Lead.
- Hub does not create, configure, or stage agents. That belongs to Agent Factory.

## Default workflow

1. Use `docs/INDEX.md` as the single documentation entry point, then read only the smallest linked document needed.
2. Inspect current files before giving code-specific advice or editing.
3. If changing project setup, architecture, runtime behaviour, documentation, backlog, automation, config, tests, environment examples, packaging, templates, AI model/provider defaults, cost logging, approval workflows, long-running workflows, or AGENTS.md, check the current project standards first if they are available.
4. Do not load the whole repository unless the task explicitly requires a broad audit.

## Navigation

- Documentation entry point: `docs/INDEX.md`
- Runtime code: `src/agent_hub/`
- Tests: `tests/`

## Runtime boundaries

- Agent registry definitions are owned by Agent Factory.
- Hub may read the staged registry from Agent Factory.
- Hub must not write registry definitions back to Agent Factory unless explicitly approved.
- Specialist implementation work should be dispatched to the appropriate specialist agent, not implemented by Hub by default.
- Telegram is an operator interface, not the source of orchestration rules.

## Universal rules

- Never guess or invent.
- Keep context bounded. Load the smallest file set that can answer the task.
- Keep work bounded and small. Touch only files required for the task.
- Do not add hidden autonomous behaviour, broad discovery loops, or silent self-improvement.
- Do not add compatibility shims, duplicate implementations, unused code, dead code, or legacy code unless explicitly requested.
- Do not hardcode hidden choices. If a prototype hardcode is explicitly approved, state why, where it lives, and what would make it configurable later.
- Do not add fallback/default behaviour that changes the outcome unless explicitly approved.
- On uncertainty, missing standards, failed validation, unavailable tools, invalid AI output, or ambiguous requirements, stop or escalate instead of silently choosing an alternate path.
- Stop and ask before destructive, broad, risky, ambiguous, expensive, repo-changing, or code-executing actions unless the human has already approved them.
- Runtime safety must be enforced in code/settings/admin, not only in instruction files.
- Do not mask failures with broad fallback logic or silent defaults.
- Do not claim completion without validation evidence or a clear reason validation was not applicable.

## Backlog

The live backlog source of truth is the Agent Hub Google Sheet referenced in README.md.

Do not create duplicate local backlog files unless explicitly requested.

## Finish report

Report only what matters when the agent finishes a task:

- Files changed
- Behaviour changed
- Validation command/result, or why not run
- Remaining risk or follow-up
