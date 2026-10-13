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

### Live acceptance status (2026-10-12; approve path proven end to end 2026-10-13)

Cancel path — PASSED. Run `fbb0096c-318d-4850-aad6-7058817ed77b` (request `5c5550f3-b9f5-41b3-8666-809dcaeeaed4`): AI Tech Lead surfaced a real approval as `waiting_decision` with options `approve`, `request_changes (needs text)`, `ask_question (needs text)`, `cancel`; the operator replied `cancel`; Hub resumed the same run; AI Tech Lead ended `terminal_failure` "Workflow completed without producing an instruction." with `execution_performed=false`. Hub did not return "No task is currently waiting for approval." Lifecycle: `asked -> routed:ai-tech-lead -> dispatched -> running -> decision -> routed -> dispatched -> running -> failed`. `/approve` and `/reject` were not exercised on a true `waiting_approval` run in this session.

Approve path — PASSED (2026-10-13, run `49fd00c5-066e-4a42-9aee-6b3f9ccbe47f`, request `68f8fa47-14c0-4348-ae0c-d7335d931a26`).

Proven end to end, with no `/stop`, no run budget, no timeout and no database edit: the decision surfaced with **no automatic specialist reply**; the options reported for this run matched the `approval` entry of `_DECISION_OPTIONS_BY_KIND` (`approve`, `request_changes`, `ask_question`, `cancel`); `/approve` and `/reject` were each refused **twice** without resuming the run; both operator `approve` replies resumed the **same run id and the same request id**; and the run then carried on through planning, a real coding-agent execution and validation to a **genuine terminal state reached through the approval path**, with **exactly one** final Telegram reply.

Superseded record: the earlier run `e222ee9a-4b97-43b1-9164-a7d98f35f635` proved the same-run resume but could not prove step 4's terminal state, because its `execute_coding_agent=false` precondition made a natural success impossible and the run was closed with `/stop`. That deviation is withdrawn — the run below used a real coding agent confined to a disposable target and needed no `/stop` at all.

Isolation, established and proven by `/proc/<pid>/environ` **before any message was sent**: a separate Hub process using AI Tech Lead's own idle bot token, which is a different Telegram update stream from the shared `@agent-hub` bot; an isolated AI Tech Lead clone on `PYTHONPATH` with a fail-closed project registry; and a disposable git target under `/tmp/opencode/053/target`. The shared `agent-hub` DM was never used. The disposable target was bootstrapped with AI Tech Lead's own `bootstrap-project-pack`, given a documented `pytest` validation command, and committed before dispatch so any later diff would be attributable to the run.

Sequence (all times AEDT, chat redacted):

- 19:30:58 bounded coding task sent; 19:31:46 the run paused at `waiting_decision` (project-guidance decision, options `approve` / `reject`) with **no automatic reply** — the ReAct loop stayed stopped, which is the AGENT-HUB-053 fix under test.
- 19:33:37 `/approve` and 19:34:26 `/reject` each answered `No task is currently waiting for approval.` and did **not** resume the run (state still `waiting_decision`, no new dispatch event).
- 19:35:02 operator replied `approve`; Hub logged `Decision 'approve' received. Resuming AI Tech Lead.` with the **same request id** `68f8fa47…`; the run moved `waiting_decision -> routed -> dispatched -> in_progress`.
- 19:35:19 the run surfaced the genuine approval interrupt — `Approval required: Orchestrator AI risk review` — with the offered set rendered as `approve`, `requestchanges (needs text)`, `askquestion (needs text)`, `cancel`, matching the `approval` entry of `_DECISION_OPTIONS_BY_KIND`.
- 19:36:08 `/approve` and 19:36:25 `/reject` were each refused a second time, on this approval decision, still without resuming (state unchanged at `waiting_decision`). `/approve` stays reserved for true `waiting_approval` runs; approval is not inferred from an "Approval required" prompt.
- 19:36:57 a second operator `approve` resumed the **same run id and the same request id** again; AI Tech Lead logged `action=approve -> REQUEST_PLAN`.
- the run then advanced through plan review, the documented validation command `pytest (documented: AGENTS.md)`, a real `codex` execution confined to a disposable task worktree, and the completion gate, pausing only on further operator decisions.
- 20:52:21 the operator's final decision reply drove the run to its terminal state `failed` (AI Tech Lead's completion gate, refusing to auto-confirm), with **exactly one** final Telegram reply at 20:52:28.

Residual finding, recorded for a human reviewer and deliberately **not** fixed here because it is an AI Tech Lead defect outside this ticket's scope: AI Tech Lead's post-approval handoff still carried the pre-approval wording "Wait for the normal human approval workflow to secure approval before implementing…", so `codex` correctly stopped without editing and AI Tech Lead logged `No new file changes detected after coding agent run`. Its completion gate then refused to auto-confirm, which is why the genuine terminal is `failed` rather than `success`. The operator verified this by inspecting the task worktree directly (clean, no diff) and rejected the completion claim honestly rather than confirming it. The approval contract itself — the subject of this ticket — is unaffected: every pause, refusal, resume and terminal above is real.

Post-run verification, matching the pre-send baseline exactly: disposable target clean; `ai-tech-lead` `22d3df9` with 0 dirty files; `agent-hub` `f5a2caf` with the same 12 pre-existing dirty files; `agent-factory` `93deb45` with the same 2 pre-existing dirty files; the shared Hub run store untouched; the shared `agent-hub` DM still on its 03:09 PM stop message. The isolated Hub was stopped afterwards and no process was left polling AI Tech Lead's bot token.

Pre-fix observation (recorded before commits `6e06087` / `a448ef9`; investigation only, no runtime change was made when it was observed) — before this branch's fix, Hub's orchestrator ReAct loop (`create_react_agent` in `orchestrator.py`) **could** treat a specialist's `waiting_decision` as an ordinary tool result and auto-reply: it re-dispatched to the specialist with synthesized guidance, with no operator input, and could repeat (three times in run `9f4fa67e` and three times in run `e48eca10`) until it emitted a final surfaced decision message or the operator sent `/stop`. That behaviour is the defect AGENT-HUB-053 fixes; the live acceptance above is what now demonstrates the loop staying stopped. It is distinct from the operator-driven `provide_decision_reply` path used in the cancel proof.

Regression lock: `tests/test_approval_decision_contract.py` (5 passed), full Hub suite **481 passed**, `ruff` clean. `tests/test_waiting_decision_react_guard.py` holds **11** tests: stream break, decision-relay verbatim reply, same-run dispatch refusal, operator resume with same request id, reserved `/approve`+`/reject`, `/stop` cancel, clarification dispatch not blocked, clarification pause not breaking the stream, concurrency bypass, real-LangGraph pause, and missing-run-record characterization. Of these, four fail if the fix is reverted (stream stop, dispatch refusal, concurrency bypass, real-LangGraph pause); the missing-run test is a pre-existing characterization guard whose `KeyError` comes from `TaskRunStore.transition`, so it is not itself evidence of the fix. Recorded on branch `test/agent-hub-053-approval-contract` (commits `dde6f40`, `48c66b0`, `38313d8`, `5716639`, `6e06087`, `a448ef9`, `84dc881`, `54f6e3c`, `e44eb1a`, `74e6bad`, `3958dda`, `74f0e5c`, merge `876e3f5` of `origin/main` `70faeb1`). Backlog: AGENT-HUB-053 is `Implemented` with human review still required because of the residual AI Tech Lead handoff finding above; nothing here has been merged.

Review note: an independent review of `origin/main...HEAD` returned REQUEST CHANGES on documentation grounds only (overclaimed verdict, stale tense, miscounts — all corrected above); no BLOCKER or code defect was raised. One MEDIUM remains open and unaddressed by design: `_serialise_run_dispatch` serializes all same-run `_call_agent` dispatches for the whole subprocess duration, so if a single ReAct superstep emitted two *different* specialists and the first paused on a decision, the sibling dispatch would be refused and dropped on resume. The parallel *fanout* tool is unaffected (it dispatches directly and fails closed on a decision-paused child). This is a documented trade-off of the TOCTOU fix, not a regression, and is currently untested.

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
