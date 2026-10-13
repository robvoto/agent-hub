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

### Live acceptance status (2026-10-12; approve-path evidence recorded 2026-10-13)

Cancel path — PASSED. Run `fbb0096c-318d-4850-aad6-7058817ed77b` (request `5c5550f3-b9f5-41b3-8666-809dcaeeaed4`): AI Tech Lead surfaced a real approval as `waiting_decision` with options `approve`, `request_changes (needs text)`, `ask_question (needs text)`, `cancel`; the operator replied `cancel`; Hub resumed the same run; AI Tech Lead ended `terminal_failure` "Workflow completed without producing an instruction." with `execution_performed=false`. Hub did not return "No task is currently waiting for approval." Lifecycle: `asked -> routed:ai-tech-lead -> dispatched -> running -> decision -> routed -> dispatched -> running -> failed`. `/approve` and `/reject` were not exercised on a true `waiting_approval` run in this session.

Approve path — PARTIAL (2026-10-13, run `e222ee9a-4b97-43b1-9164-a7d98f35f635`).

Proven: steps 1, 3 and 4's *same-run resume*, plus step 2's option list for this run. Specifically — the decision surfaced with no automatic reply; the options reported for this run matched the `approval` entry of `_DECISION_OPTIONS_BY_KIND`; `/approve` and `/reject` were refused without resuming the run; and both operator `approve` replies resumed the same run id into the next graph node. (Step 2's `Decision needed: …` listing was captured for run `0f2b2e3a`, which reached a clarification decision rather than the approval one, so the approval-run listing is evidenced by the options reply below instead.)

Not proven as written: step 4's "reaches a terminal state" *through the approval outcome*. The coding-agent-disabled precondition this section demands makes a natural success state impossible for a `coding_task` (see Structural note below), so the run was closed with `/stop` (`cancelled`) instead. The "exactly one final reply" observation therefore attaches to that `/stop` confirmation, not to an approval result, and the approval resume itself produced intermediate replies (the next decision prompt, then `empty plan`). The deviation is structural rather than a defect in the resume path, but it does mean step 4 is satisfied only in part and a human reviewer should decide whether that is acceptable.

Safety precondition, established before any message was sent: `execute_coding_agent=false` with `coding_agent_command=/bin/true`, held for the whole window by a 1 s watchdog that appended every observation to an audit log. That log records only its own `START`/`OBSERVED` lines — **zero reverts** — so the silent-rewrite failure that blocked the 2026-10-12 attempt did not recur. No `codex` process appeared at any point, and `ai-tech-lead`, `agent-hub`, `agent-factory` plus the throwaway target (`15354c9`) were byte-identical to a pre-send git baseline. This is the "coding agent reliably disabled" condition the 2026-10-12 note demanded. Settings were restored to `execute_coding_agent=true` / `codex` afterwards.

Sequence (all times AEDT, chat redacted):

- 14:58:14 bounded task sent; 14:58:33 run paused at `waiting_decision` (project-guidance decision) with **no automatic reply** — the ReAct loop stayed stopped, which is the AGENT-HUB-053 fix under test.
- 15:02:02 operator replied `approve`; Hub logged `Decision 'approve' received. Resuming AI Tech Lead.` and `waiting_decision -> routed`, resuming the **same run id** into risk review.
- 15:02:21 the run reached the genuine approval interrupt: `Approval required: Orchestrator AI risk review`, lifecycle `… -> [decision] -> [routed] -> [dispatched] -> [running] -> [decision]`.
- 15:03:31 an invalid reply reported the offered set (the log excerpt below shows it with the `[AI Tech Lead] ` prefix that `orchestrator.py` prepends): `[AI Tech Lead] 'bogus-option' is not a valid option right now. Valid options: approve, ask_question, cancel, request_changes.` — the option list matches `", ".join(sorted(allowed))` for the `approval` entry of `_DECISION_OPTIONS_BY_KIND`.
- 15:04:05 `/approve` and 15:04:30 `/reject` each answered `No task is currently waiting for approval.` and did **not** resume the run (lifecycle still ended at the 15:02:21 `[decision]`). `/approve` stays reserved for true `waiting_approval` runs; approval is not inferred from an "Approval required" prompt.
- 15:05:0x a second operator `approve` resumed the same run again, which advanced and reported `The coding agent returned an empty plan` — direct evidence the execution lock was in force.
- 15:09:07 `/stop` closed the run as `cancelled` with `finished_at` set and **exactly one** final Telegram reply (`Stopped run e222ee9a-… State is now cancelled.`).

Structural note: with the coding agent disabled the plan node always returns an empty plan, and `coding_workflow_graph.py` resets `plan_rejection_count` to `0` on every guidance reply, so a `coding_task` cannot reach a natural success state while execution is disabled. The run was therefore closed with `/stop`; the same-run-resume behaviour required by step 4 was proven directly by both `approve` replies.

Hazard avoided during this session: running Hub with a disposable `AGENT_FACTORY_ROOT` that lacked the real `agent_factory` package made a concurrent AF-048 Agent Factory task fail with `ModuleNotFoundError`. Hub was switched back to the real factory root for the remainder; the settings lock alone is sufficient to keep the coding agent disabled.

Pre-fix observation (recorded before commits `6e06087` / `a448ef9`; investigation only, no runtime change was made when it was observed) — before this branch's fix, Hub's orchestrator ReAct loop (`create_react_agent` in `orchestrator.py`) **could** treat a specialist's `waiting_decision` as an ordinary tool result and auto-reply: it re-dispatched to the specialist with synthesized guidance, with no operator input, and could repeat (three times in run `9f4fa67e` and three times in run `e48eca10`) until it emitted a final surfaced decision message or the operator sent `/stop`. That behaviour is the defect AGENT-HUB-053 fixes; the live acceptance above is what now demonstrates the loop staying stopped. It is distinct from the operator-driven `provide_decision_reply` path used in the cancel proof.

Regression lock: `tests/test_approval_decision_contract.py` (5 passed), full Hub suite **481 passed**, `ruff` clean. `tests/test_waiting_decision_react_guard.py` holds **11** tests: stream break, decision-relay verbatim reply, same-run dispatch refusal, operator resume with same request id, reserved `/approve`+`/reject`, `/stop` cancel, clarification dispatch not blocked, clarification pause not breaking the stream, concurrency bypass, real-LangGraph pause, and missing-run-record characterization. Of these, four fail if the fix is reverted (stream stop, dispatch refusal, concurrency bypass, real-LangGraph pause); the missing-run test is a pre-existing characterization guard whose `KeyError` comes from `TaskRunStore.transition`, so it is not itself evidence of the fix. Recorded on branch `test/agent-hub-053-approval-contract` (commits `dde6f40`, `48c66b0`, `38313d8`, `5716639`, `6e06087`, `a448ef9`, `84dc881`, `54f6e3c`, `e44eb1a`, `74e6bad`, merge `876e3f5` of `origin/main` `70faeb1`). Backlog: AGENT-HUB-015 and AGENT-HUB-053 both remain `Blocked` pending human review of this evidence.

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
