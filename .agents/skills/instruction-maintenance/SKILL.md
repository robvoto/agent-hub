---
name: instruction-maintenance
description: Use only when editing Agent Hub instruction files: AGENTS.md, project skills, docs, diagrams, or other markdown that defines agent workflow or repo ownership. Do not use for runtime/product changes unless instruction cleanup is part of the task.
---

# Skill: Instruction Maintenance

Use when editing `AGENTS.md`, `.agents/skills/*/SKILL.md`, or important project docs that guide agents.

## Purpose

Keep instructions useful, small, current, and non-contradictory.

## Source hierarchy

- `AGENTS.md`: always-loaded repo rules and routing.
- `.agents/skills/*/SKILL.md`: compact task-specific rules loaded only for that work area.
- `docs/*`: human/reference documentation, not always-loaded operating rules unless explicitly linked.

## Cleanup rules

- Prefer deleting or moving stale instruction noise over adding more instructions.
- Remove stale architecture claims when verified wrong.
- Only document Hub-owned flows in this repo.
- Avoid duplicating the same rule across `AGENTS.md`, skills, and docs.
- If a skill grows too large, keep `SKILL.md` compact and move detailed reference material into normal docs.
- If unsure whether information is stale, mark it for review instead of rewriting it as fact.
- For Mermaid interaction diagrams, prefer one user-visible path per diagram instead of stacking unrelated `alt` branches into one large sequence.
- Keep Mermaid note text short and renderer-safe: plain wording, explicit `<br/>` breaks, and no dense paragraph-style note blocks.
- When editing a `.mmd`, rerender the matching `.svg` and treat a render failure as a doc bug to fix, not as a reason to leave the SVG stale.
- Prefer human-facing labels in diagrams. Put real file names or Python method names in short supporting prose only when they genuinely clarify the behavior.

## Audit checklist

- Does this rule still match current architecture?
- Is it actionable for an agent?
- Is it in the right file?
- Is it duplicated elsewhere?
- Does it accidentally encourage hardcoding, fallback logic, or broad rewrites?

## Safe edit pattern

1. Inspect current files first.
2. Make small targeted edits.
3. Delete out-of-scope docs/diagrams when they do not belong to Agent Hub.
4. Report exactly what changed and what was left alone.

## Cross-tool sharing

``.agents/skills/` and `AGENTS.md` are shared repository instruction owners. Do not relocate shared skills into an agent-specific adapter path to satisfy one runtime. If a runtime needs a bootstrap adapter, that adapter may point into the shared routing; the shared project must not depend on the adapter.

## Do not

- Do not rewrite all instructions in one pass.
- Do not add broad inspirational guidance.
- Do not turn backlog rows into operating rules.
