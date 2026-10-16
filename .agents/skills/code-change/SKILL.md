---
name: code-change
description: Use for Agent Hub source, test, runtime, or integration changes and read-only code reviews assigned to ATL or Codex. Pair with the owning domain skill, especially hub-runtime-change for Hub behavior.
---

# Skill: Code Change

Use with `AGENTS.md` and the smallest matching project skill.

- For Hub routing, dispatch, lifecycle, approvals, cancellation, CLI, or Telegram behavior, read `hub-runtime-change/SKILL.md` before editing. For MCP/MCPO work, also read `human-mcp-access/SKILL.md` and trace agent, proxy and server hops separately.
- Check the current branch, worktree, and dirty paths before editing. Use an isolated task branch/worktree when available; preserve other workers' changes and do not clean files you do not own.
- Inspect the exact source and tests first. Keep changes scoped to the accepted task contract, test meaningful behavior, and report the exact validation performed.
- For shared routing, persistence, or operator-flow changes, run the relevant tests and the full Hub suite. Run `git diff --check` before committing.
- For read-only review requests, inspect the supplied diff and report actionable findings with file/line references. Do not modify files, commit, or push.
- For substantial tasks with genuinely independent workstreams, proactively use the available subagent interface to delegate bounded research, read-only review, or isolated implementation when it will materially speed or improve the work. Give each worker one owner, a precise scope, and a concrete output; keep independent review read-only. Parallel edits require separate task worktrees and disjoint file/resource ownership. The owning task agent integrates results, validates the combined change, and retains approval and release gates. Do not delegate duplicate work or create/enable runtime agents as part of this guidance.
- Use only models/providers and execution budgets explicitly configured and authorized in the current environment. Verify a worker route before use; never infer API access or free usage from a ChatGPT plan or a model label, silently switch providers/models, or exceed an approved budget. If delegation or a selected model is unavailable, say so and proceed only within the authorized route.
- Keep ATL review plans within its existing gate of five bullets and 120 words. That is a plan readability check, not an aggregate token/time budget. Do not invent task-wide token caps or per-call output caps; preserve the declared budget and report measured usage.
- After the first MCP failure, always make one safe recovery retry before calling it a blocker. Retry idempotent reads once; for a 400 or malformed request, refresh the live schema and protocol/session details, correct the request, then retry once instead of resending the same bad payload. For uncertain mutations, inspect the outcome before replaying. Never retry a session-cleanup `DELETE` or restart shared MCP without explicit authorization.
