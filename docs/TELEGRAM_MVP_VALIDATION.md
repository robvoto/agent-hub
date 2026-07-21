# Telegram MVP Validation

Use this runbook to execute and document the real end-to-end Agent Hub MVP proof once live Telegram credentials are available.

## Purpose

Prove the bounded real workflow:

```text
Telegram -> Agent Hub -> AI Tech Lead -> Agent Hub -> Telegram
```

This validation is required for `HUB-MVP-005`.

## Prerequisites

The following environment values must be configured before starting:

```text
OPENAI_API_KEY=...
HUB_BOT_TOKEN=...
HUB_ALLOWED_CHAT_IDS=...
```

`HUB_ALLOWED_CHAT_IDS` must include the Telegram chat ID used for the proof.

## Preflight

Run from WSL:

```bash
cd ~/projects/agent-hub
uv sync
uv run agent-hub telegram --debug
```

Expected result:

- startup health passes
- `AI Tech Lead` is present in the registry validation output
- the Telegram gateway starts polling without configuration failures

If startup fails, do not continue the proof. Fix the reported failure first.

## Proof Steps

Use one Telegram chat that is included in `HUB_ALLOWED_CHAT_IDS`.

1. Send `/agents`
2. Confirm `AI Tech Lead` appears in the reply
3. Send a bounded coding request that should route to `AI Tech Lead`
4. If the agent asks a clarification question, reply in the same chat and confirm the same task resumes
5. If the agent requests approval, confirm Telegram returns the approval message
6. Send `/approve` and confirm the same task resumes
7. Run a second bounded request that you intentionally reject if approval is requested
8. Send `/reject <reason>` and confirm the paused task closes safely
9. Send `/status` during active or paused work and confirm the current run details are shown
10. Send `/last` after a completed or failed run and confirm the last run details are shown
11. If a long-running task is active, send `/stop` and confirm the run moves to `cancelled`
12. Send `/learn test learning from telegram`
13. Send `/memory` and confirm the learning appears with a stable identifier
14. Send `/forget <identifier>` and confirm the learning is removed

## Evidence To Capture

Capture enough evidence to prove the real flow without exposing secrets:

- terminal output from `uv run agent-hub telegram --debug`
- Telegram screenshots or copied replies for:
  - `/agents`
  - routing to `AI Tech Lead`
  - clarification flow if triggered
  - approval flow and `/approve`
  - rejection flow and `/reject`
  - `/status`
  - `/last`
  - `/stop` if exercised
  - `/learn`, `/memory`, and `/forget`
- relevant rows from `data/task_runs.sqlite3`
- relevant entries from `data/llm_usage.json`
- relevant lines from `logs/agent-hub.log`

Do not capture:

- raw API keys
- bot tokens
- authorization headers
- oversized prompts

## Minimum Success Criteria

The run is valid only if all of the following are true:

- Agent Hub starts successfully in Telegram mode
- `/agents` lists `AI Tech Lead`
- a bounded Telegram request routes to `AI Tech Lead`
- clarification and approval behavior works if requested by the specialist
- `/approve` resumes the paused task
- `/reject` safely closes the paused task
- `/status` and `/last` return persisted run information
- `/stop` cancels active work without silently converting it to `failed`
- `/learn`, `/memory`, and `/forget` work against the Hub knowledge store
- task lifecycle is persisted in `data/task_runs.sqlite3`
- token usage and estimated cost are recorded in `data/llm_usage.json`
- logs remain human-readable and do not expose secrets

## Validation Notes

Record the final evidence location and outcome in the live backlog row for `HUB-MVP-005`.
