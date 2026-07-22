---
name: agent-hub-work
description: Use ONLY for repo-boundary questions in Agent Hub — what belongs here vs. Agent Factory vs. a specialist repo. Do NOT use for the mechanics of a runtime code change (see hub-runtime-change) or backlog rows (see backlog-management).
---

# Skill: Agent Hub Work

Use this skill when working in `agent-hub` and you need the repo-specific ownership model.

## Core boundary

- Agent Hub owns orchestration, operator interfaces, task state, approvals, memory, logs, and specialist dispatch.
- Agent Factory owns agent creation, staging, and registry definitions.
- Specialist repos own specialist-internal workflows, prompts, and implementation details.
- If a doc, diagram, or code path describes specialist-internal behavior rather than Hub-owned behavior, remove it from this repo or reduce it to the Hub-facing boundary.

## Default workflow

1. Start at `docs/INDEX.md`, then read only the smallest linked file needed.
2. Confirm the requested change belongs to Agent Hub before editing code or docs.
3. Inspect the current implementation before changing anything.
4. Keep edits bounded. Touch only files required for the task.

## Hub-owned runtime model

- Canonical operator path: user -> Hub CLI or Telegram -> `HubOrchestrator` -> selected specialist -> Hub response.
- Registered specialists are read from Agent Factory; Hub does not write those definitions back unless explicitly approved.
- Telegram is an operator interface, not the source of orchestration rules.
- Do not add hidden autonomous behavior, silent fallbacks, compatibility shims, or duplicate ownership unless explicitly approved.

## Docs and diagrams

- Only document Hub-owned flows in this repo.
- Verify every referenced command, file, and entrypoint exists in this repo.
- If a diagram has drifted and the concept does not belong to Agent Hub, delete it rather than polishing it.

## Cross-repo boundary in practice

- Hub may propose a change to a specialist repo's contract (e.g. "ai-tech-lead should also accept X") — but the actual code/config change happens in that repo, by that repo's own agent/session, not by editing files there directly from a Hub-focused session unless the human explicitly asks for it.
- If a fix genuinely requires touching Agent Factory or a specialist repo (e.g. expanding an allowlist, aligning a manifest field), say so plainly, do the work if asked, but keep the commit in that repo separate from Hub's own commits — don't bundle cross-repo changes into one Hub commit.
- Permission/allowlist expansions (which projects a specialist may write to) are a real security decision, not a default — confirm which paths before writing them anywhere.

## Checklist

- Is this Hub's call, or does it actually belong to Agent Factory / a specialist repo?
- If cross-repo, did you commit each repo's change separately, in that repo, not smuggled into Hub's diff?
- Does a docs/diagram change describe Hub-owned behavior only, not specialist internals?
