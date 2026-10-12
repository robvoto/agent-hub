# Agent Hub validation

Manual end-to-end checks for changes that automated tests cannot prove through the real CLI, Telegram transport, credentials and specialist processes.

Use only the sections affected by the change. Record final evidence in the relevant live backlog row. Do not collect secrets.

## Preflight

```bash
cd ~/projects/agent-hub
uv sync
uv run pytest -q
uv run agent-hub --debug telegram
```

Stop if startup health fails or the required specialist is unavailable.

## Core transport and routing

1. `/hub-status` reports the current session, project, learning mode and callable agents.
2. `/agents` lists expected callable specialists.
3. Send one bounded task and confirm immediate acknowledgement.
4. Confirm meaningful specialist progress appears without repeated “still working” noise.
5. `/status` shows current phase, latest progress and last specialist activity.
6. Confirm the final result arrives once and the run is persisted.
7. `/last` shows the terminal run.

## Pause, resume and cancellation

Exercise only paths supported by the selected specialist. Treat the specialist protocol as authoritative: `approval_required` / `waiting_approval` uses `/approve` and `/reject`; `waiting_decision` uses the reported option number or name, even when the prompt itself says "approval". Do not infer approval semantics from wording.

- clarification: reply normally and confirm the same run resumes;
- approval: `/approve` resumes the same `waiting_approval` run;
- rejection: `/reject <reason>` closes the same `waiting_approval` run safely;
- decision: reply with the reported option number or name and confirm the same `waiting_decision` run resumes;
- cancellation: `/stop` persists `cancelled`, terminates any live specialist child process tree, and produces no late final reply;
- reset: `/reset` cancels active work and rotates to a fresh thread.

Changing `/project` while a task is paused must not change the project context pinned to that task.

## Specialist approval-decision acceptance (AGENT-HUB-015 / AGENT-HUB-053)

Purpose: prove the AI Tech Lead approval path in real Telegram, and confirm Hub keeps `/approve` and `/reject` reserved for true `waiting_approval` runs.

Preflight: `uv run pytest -q`; start `uv run agent-hub --debug telegram`; `/agents` lists AI Tech Lead; select a target project.

Approve — same-run resume via the reported option:

1. Send a bounded coding task that reaches an AI Tech Lead approval pause.
2. Confirm the reply is `[AI Tech Lead] Decision needed: …` listing `approve`, `request_changes (needs text)`, `ask_question (needs text)`, `cancel`. Record the run id; `/status` shows `waiting_decision`.
3. While paused, send `/approve` and `/reject`. Each must reply `No task is currently waiting for approval.` and must not resume the run — this proves those commands stay reserved and that approval is not inferred from the prompt.
4. Reply `approve`. The same run resumes (same run id and request id) and reaches a terminal state; exactly one final reply, no duplicates.
5. Confirm the same run id in `task_runs.sqlite3` moves `waiting_decision → routed → dispatched → in_progress → …`.

Cancel — safe close via the reported option:

1. Send another bounded task to the AI Tech Lead approval pause; `/status` shows `waiting_decision`.
2. Reply `cancel`. The run closes without executing the action and no late result is delivered.

Reserved-path guard: on a true `waiting_approval` run (Factory or Human MCP), `/approve` resumes and `/reject <reason>` closes it, confirming those commands still work for their own path.

Evidence: Telegram replies (redacted), run ids and states, bounded log excerpts, and the `uv run pytest -q` result. Never capture bot tokens, API keys or full prompts.

### Live acceptance status (2026-10-12)

Cancel path — PASSED. Run `fbb0096c-318d-4850-aad6-7058817ed77b` (request `5c5550f3-b9f5-41b3-8666-809dcaeeaed4`): AI Tech Lead surfaced a real approval as `waiting_decision` with options `approve`, `request_changes (needs text)`, `ask_question (needs text)`, `cancel`; the operator replied `cancel`; Hub resumed the same run; AI Tech Lead ended `terminal_failure` "Workflow completed without producing an instruction." with `execution_performed=false`. Hub did not return "No task is currently waiting for approval." Lifecycle: `asked -> routed:ai-tech-lead -> dispatched -> running -> decision -> routed -> dispatched -> running -> failed`. `/approve` and `/reject` were not exercised on a true `waiting_approval` run in this session.

Approve path — NOT PROVEN. A live `approve` could not be exercised safely: the fail-closed setting (`execute_coding_agent=false`) was silently reverted mid-session (the settings file was rewritten by the ATL admin server/operator) and even read-only recon starts a real coding-agent (`codex`) process, so an approval could execute code. Do not claim the approve path until it is proven with the coding agent reliably disabled.

Observed behaviour (investigation only, no runtime change) — Hub's orchestrator ReAct loop (`create_react_agent` in `orchestrator.py`) can treat a specialist's `waiting_decision` as an ordinary tool result and auto-reply: it re-dispatches to the specialist with synthesized guidance, with no operator input, and can repeat (three times in run `9f4fa67e` and three times in run `e48eca10`) until it emits a final surfaced decision message or the operator sends `/stop`. This is distinct from the operator-driven `provide_decision_reply` path used in the cancel proof.

Regression lock: `tests/test_approval_decision_contract.py` (5 passed), full Hub suite 468 passed, `ruff` clean. Recorded on branch `test/agent-hub-053-approval-contract` (commits `dde6f40`, `48c66b0`, `38313d8`). Backlog: AGENT-HUB-015 and AGENT-HUB-053 both remain `Blocked`.

## Short-term memory

1. Start with `/new`.
2. Tell Hub a temporary fact without `/learn`.
3. Ask about it in the same thread; Hub should retain it.
4. Run `/new` and ask again; the temporary fact should not be available from the new thread unless it was also stored as long-term memory.
5. Restart Hub during a test thread when checkpoint persistence is under test; the same thread should recover.

## Long-term memory

1. `/learn Prefer concise answers and never guess.`
2. Confirm the reply contains the stored memory identifier and analysis outcome.
3. `/memory` must show the record and type/status.
4. Run `/new`.
5. Ask how Hub should answer; the stored instruction should still apply when relevant.
6. `/forget <memory-id>` removes it.
7. `/memory` no longer shows it.

## Governed runtime skills

Use a harmless, repeatable procedural lesson likely to classify as a skill.

1. Run `/learn <lesson>`.
2. Confirm the response states whether a skill was created, updated or rejected.
3. Repeat with the exact same intended skill slug and a refined procedure; it should create a new version, not a duplicate skill.
4. A conflicting duplicate title under another slug must be rejected.
5. Confirm memory storage still exists even if skill creation is rejected.

Do not confuse runtime skills in the knowledge store with repository `.agents/skills/` files.

## Proposal-only outcomes

For a lesson that implies documentation, backlog, code or a new specialist:

- Hub may classify and recommend the next action;
- Hub must not edit files, write the backlog, change runtime code or create an Agent Factory package automatically;
- the response must state that approval or another governed workflow is required.

## Evidence

Capture only what the relevant backlog item needs:

- test command and result;
- CLI or Telegram replies;
- task-run identifiers and relevant states;
- bounded log excerpts;
- memory or skill identifiers;
- exact failure if validation stops.

Never capture API keys, bot tokens, authorisation headers or entire oversized prompts.
