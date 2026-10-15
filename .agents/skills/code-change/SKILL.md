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
- Keep ATL review plans within its existing gate of five bullets and 120 words. That is a plan readability check, not an aggregate token/time budget. Do not invent task-wide token caps or per-call output caps; preserve the declared budget and report measured usage.
- After the first MCP failure, always make one safe recovery retry before calling it a blocker. Retry idempotent reads once; for a 400 or malformed request, refresh the live schema and protocol/session details, correct the request, then retry once instead of resending the same bad payload. For uncertain mutations, inspect the outcome before replaying. Never retry a session-cleanup `DELETE` or restart shared MCP without explicit authorization.
