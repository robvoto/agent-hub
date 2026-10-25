---
name: hub-runtime-change
description: Use ONLY for code/test/runtime changes to Hub's own orchestrator, dispatch, task lifecycle, or operator-interface behavior. Do NOT use for Agent Factory or specialist-repo internals, or for backlog-only/instruction-only edits.
---

# Skill: Hub Runtime Change

Use before changing Hub runtime code.

## Owners

Read the owner module before editing. If one module clearly owns the behavior, start there and stop expanding once the source of truth is found.

- `src/agent_hub/orchestrator.py`: routing, dispatch, approvals, clarification resume, task finalization, concurrency/busy-project checks, LangGraph thread_id
- `src/agent_hub/task_runs.py`: persisted lifecycle states, per-project run lookups
- `src/agent_hub/task_control.py`: cancellation/active subprocess control
- `src/agent_hub/registry.py`: specialist registry loading (reads Agent Factory's staged config, never writes it)
- `src/agent_hub/runtime_policy.py`: runtime.mode contract validation for callable specialists
- `src/agent_hub/manifest_cache.py`: specialist manifest fetch/cache by hash and TTL
- `src/agent_hub/project_context.py`: per-session "current project" selection (`/project`)
- `src/agent_hub/telegram_gateway.py`: Telegram operator flow
- `src/agent_hub/cli.py`: CLI operator flow

## Rules
- If the design introduces or relies on a heuristic/approximate inference, apply the global `heuristic-review` skill before coding. Assistive heuristics are allowed only when an independent LLM/authoritative validation layer controls the final outcome; authoritative heuristics require explicit human approval.

- Keep changes small and scoped.
- Explain internal contract fields in operator language. Do not present names such as `runtime.mode=manual` as though the user chose or should understand them. Say what Hub can or cannot do, own the implementation work, and mention the field only as brief diagnostic detail when useful. For example: “This staged agent is not set up for Hub to run yet. I need to add its Hub adapter; you do not need to configure that internal field.”
- For a substantial runtime, Telegram, dispatch, or staging boundary change, use a distinct read-only reviewer as required by `code-change/SKILL.md`; do not count the implementation agent's own inspection as independent review.
- Do not implement specialist-internal logic inside Hub — a contract test double speaks only the runtime shape (status/result_kind/etc.), never real specialist reasoning.
- Do not add fallback/default behavior that changes outcomes unless explicitly approved.
- Do not mask invalid agent output with guessed status mappings or silent recovery.
- Preserve explicit lifecycle semantics: received, routed, dispatched, in_progress, waiting_*, succeeded, failed, cancelled.
- Treat specialist protocol status as authoritative. `approval_required` maps to Hub approval commands; structured `pending_decision` maps to `waiting_decision` and must be resumed with one of the reported options. Never infer approval behavior from prompt text or a specialist-internal decision kind.
- If a specialist status cannot be resumed cleanly by Hub, surface that explicitly instead of inventing a resume path.
- Any path that resumes a live subprocess after a pause must register that run with `TaskControlRegistry` for the resumed dispatch. Cancellation is incomplete unless `/stop` terminates the process tree, persists `cancelled`, and suppresses any late result.
- If a change lets more than one task run at once (new dispatch path, new concurrency mode), check whether it shares LangGraph `thread_id`/session state with another concurrent path — two invokes sharing one thread can interleave conversation state. This was a real bug caught mid-build, not a hypothetical.
- Cross-repo permission/allowlist decisions (which projects a specialist may write to) are not Hub's call — they live in the specialist's own local settings and Agent Factory's registry, not Hub code.
- `human_logger` output (`_consume_graph_stream_event` in `orchestrator.py`) must not repeat the same fact via multiple LangGraph `stream_mode`s — e.g. don't reintroduce "task started"/"task finished" lines alongside "entered node"/"produced message", they say the same thing twice per node.
- Node-level explanations of what a LangGraph node does (`_NODE_EXPLANATIONS`/`_node_intro`) are shown once per task run, not on every loop iteration — if you add a new node type, add its explanation to `_NODE_EXPLANATIONS`, but don't make it repeat on reroutes.
- `config/llm_costs.json` fails closed by design: a model entry with `"status": "unknown"` means nobody has verified its per-1M pricing yet, not that cost tracking is broken. Fix by adding real rates (cross-check the source), don't add fallback/guessed pricing.

## Factory handoff debugging

- A Factory `success` result may have `next_task: null` when no implementation task has been approved. Treat null as no follow-on; do not validate it as a `NextTaskContract`, label it malformed, or bypass the Factory approval pause. Keep a regression test for this contract.
- Hub's interactive CLI consumes one user turn per input line. Send exactly one request line; never paste multiline instructions into the prompt. After accidental extra input or a failed call, inspect the exact run and task state before retrying or cancelling anything.
- Do not feed the interactive CLI with a heredoc or here-string: the delimiter can arrive as another user turn. Use a controlled interactive session or encode the single request line so only that line reaches stdin; then inspect the task list for accidental extra turns.
- For a transient or malformed request, inspect the live contract and state, correct the request, and retry once when safe. Do not repeat identical payloads or create a duplicate Factory task while the original thread/correlation is still usable.
- A missing child-process environment variable is not evidence that a secret file must be opened. Check configuration and inheritance without printing secret values or asking the operator to paste them.

## Checklist

- Which module owns this behavior — did you stop there, or drift into unrelated files?
- Does every branch on `status` also have a plan for `failed` (terminal, no resume)?
- If this touches session/project/task-run scoping, does `/status` and `/last` still behave as whole-conversation (not per-project) unless a decision explicitly changed that?
- Did a new concurrency path get a real test against something closer to a real subprocess, not just a mocked `Popen`?
- Is there human-readable log output (`human_logger`) at the decision points an operator would want to see live, not just debug-level detail — and does it say each fact exactly once per event?

## Definition of Done

A Hub runtime change is done only when:
1. The smallest relevant test slice passes; broaden to the full suite for shared routing, persistence, approvals, startup, or operator-interface changes.
2. Tests are added or updated when behavior changes.
3. No unapproved fallback, hardcoding, or specialist-internal logic was introduced.
4. Human-readable logging exists for any new operator-visible decision.
5. Changes to pause/resume/cancellation are live-proven against the real operator path when automated tests cannot prove subprocess termination or late-reply suppression.

## Finish format

Report:
- Files changed
- Behavior changed
- Validation command/result
- Remaining risk or follow-up (e.g. "needs a live smoke test" — flag it, don't claim it's proven)


## Prove a specialist dispatch end to end

An agent listed in the registry is registered, not necessarily eligible. Confirm its manifest command and task contract declare the task kind Hub routes before treating it as callable. For acceptance, verify the current Hub and Factory revisions, correlate route selection with dispatch and specialist output, and inspect the exact task payload the subprocess received.

Keep literal task input separate from caller context. If Hub rewriting adds context to `task`, the specialist's exact echo is correct for the composite payload but the user's literal-input acceptance has not passed. Test the actual contract and describe precisely which payload was accepted. When another worker owns the Telegram bot conversation, open a workflow-owned tab/window and use a separate supported chat/session; a fresh tab alone does not isolate the bot conversation. Use Hub's native specialist interface for agent dispatch; use MCP only when no suitable native interface exists.
