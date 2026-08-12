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
- Project skills: `.skills/`
- Runtime code: `src/agent_hub/`
- Tests: `tests/`

## Skill selection

Use the most relevant project skill from `.skills/` for bounded work in this repo.

Reusable defaults:

- `agent-hub-work`: repo boundary, orchestration ownership, hub-vs-specialist scope
- `hub-runtime-change`: orchestrator, Telegram, CLI, task lifecycle, dispatch, approvals
- `instruction-maintenance`: AGENTS, skills, docs, diagrams, and stale instruction cleanup
- `human-mcp-access`: discover and use the repo-configured Human MCP server for Google Sheets/Docs
- `backlog-management`: creating/updating/grooming rows in the live backlog Google Sheet; load `human-mcp-access` first

## Governed self-improvement

- The agent may improve its own reusable skills or `AGENTS.md` without separate approval when evidence from completed work shows a repeatable problem, recurring correction, avoidable rework, or stable procedure.
- Keep every improvement bounded to the demonstrated problem. Do not broaden Hub ownership, specialist ownership, permissions, memory access, tool access, runtime authority, or repository scope.
- Before editing, record the evidence, target file, expected reusable benefit, risk, and validation method in the task trace or final report.
- Reusable skills must remain concise and procedural, be registered in the relevant `.skills/` index, and be validated with the smallest relevant test or deterministic check.
- Do not duplicate policy across skills and `AGENTS.md`. Put universal behavioural rules in `AGENTS.md`; put task-specific procedures in skills.
- Code or runtime self-modification still requires the normal approved bounded coding workflow and relevant validation.
- Stop without changing anything when the evidence, target, ownership, or validation method is unclear.

## Runtime boundaries

- Agent registry definitions are owned by Agent Factory.
- Hub may read the staged registry from Agent Factory.
- Hub must not write registry definitions back to Agent Factory unless explicitly approved.
- Specialist implementation work should be dispatched to the appropriate specialist agent, not implemented by Hub by default.
- Telegram is an operator interface, not the source of orchestration rules.

## Universal rules

- Never guess or invent.
- Before introducing or relying on heuristic/approximate inference, use the global `heuristic-review` guardrail. Assistive heuristics may help an LLM or reduce search cost when they cannot determine the final outcome; heuristics that decide semantic meaning, business outcome, target, permission, or action require explicit human approval.
- Keep context bounded. Load the smallest file set that can answer the task.
- Keep work bounded and small. Touch only files required for the task.
- Do not add hidden autonomous behaviour, broad discovery loops, or uncontrolled self-improvement.
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
- Self-improvement evidence and validation, when applicable
- Validation command/result, or why not run
- Remaining risk or follow-up
