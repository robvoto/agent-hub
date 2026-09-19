# Agent Instructions

Minimal shared routing instructions for Agent Hub. This file is not the project manual, architecture guide, backlog, standards document, skill catalogue, or test plan.

## Project boundary

Agent Hub is the runtime orchestrator/control plane. It receives operator input, reads staged agent definitions, and dispatches work to specialists. Agent creation/configuration/staging belongs to Agent Factory, not Hub.

## Default workflow

1. Use `docs/INDEX.md` to find the smallest relevant project document.
2. Use `.agents/skills/INDEX.md` to choose the smallest relevant task skill.
3. Inspect current files before editing or giving code-specific advice.
4. Check current project standards before changing project/instruction structure, architecture, runtime, automation, config, tests, packaging, models/providers, costs, or approval workflows.
5. For branch/worktree, commit, push, PR, merge, or `main` integration, use `.agents/skills/git-lifecycle/SKILL.md`.
6. Do not load the whole repo unless the task requires a broad audit.

## Durable rule placement

- Shared project rules must be runtime-neutral.
- Put task-specific procedures in `.agents/skills/` and register them in `.agents/skills/INDEX.md`.
- If no skill owns a durable rule, create a focused shared skill rather than expanding `AGENTS.md`.
- Agent-specific adapter files, when present, own only themselves. Shared docs/tests must not enumerate, require, or depend on specific adapter filenames.

Use `.agents/skills/instruction-maintenance/SKILL.md` for instruction structure changes.

## Runtime boundaries

- Agent registry definitions are owned by Agent Factory; Hub may read staged definitions but must not write them back unless explicitly approved.
- Specialist implementation work should be dispatched to the appropriate specialist by default.
- Telegram is an operator interface, not the source of orchestration policy.
- Runtime Hub skills stored in the knowledge store are product data; `.agents/skills/` contains repository instructions for coding agents. Do not conflate them.

## Universal rules

- Never guess or invent; inspect authoritative sources first.
- Keep context and changes bounded to the task.
- Do not add hidden autonomous behaviour, broad discovery loops, compatibility shims, duplicate implementations, dead code, or outcome-changing fallbacks unless explicitly approved.
- Do not hardcode hidden choices that belong in config/schema/managed knowledge.
- Heuristics that determine semantic meaning, business outcome, target, permission, or action require explicit human approval; assistive heuristics may only support an authoritative path.
- Runtime safety must be enforced in code/settings/admin, not only prose.
- Stop/escalate on uncertainty or failed validation rather than silently choosing an alternate path.
- Do not claim completion without validation evidence.
- Preserve unrelated concurrent work.
- Before editing, inspect the exact current target file and apply a narrow, context-checked patch.
- If a patch hunk or `old_text` does not match, stop and reread the file before creating a new patch; never retry stale patch text.
- After editing, inspect the diff and run the required validation before reporting completion.

## Backlog

The live Agent Hub backlog is the Google Sheet referenced in project docs. Do not create a competing local backlog.

## Finish report

Report what changed, validation performed/result, remaining risk/follow-up, and Git integration state when relevant.
