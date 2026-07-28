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

Exercise only paths supported by the selected specialist:

- clarification: reply normally and confirm the same run resumes;
- approval: `/approve` resumes the same run;
- rejection: `/reject <reason>` closes it safely;
- decision: a normal reply with the option number or name resumes the same run;
- cancellation: `/stop` marks active work cancelled;
- reset: `/reset` cancels active work and rotates to a fresh thread.

Changing `/project` while a task is paused must not change the project context pinned to that task.

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

Do not confuse runtime skills in the knowledge store with repository `.skills/` files.

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
