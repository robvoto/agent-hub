# Telegram MVP Validation

Use this runbook to execute and document the real end-to-end Agent Hub MVP proof once live Telegram credentials are available.

## Purpose

Prove the bounded real workflow:

```text
Telegram -> Agent Hub -> AI Tech Lead -> Agent Hub -> Telegram
```

This validation is required for `AGENT-HUB-015`.

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
9. Confirm Telegram sends an immediate start acknowledgement for the routed task
10. During active work, confirm Telegram shows meaningful live progress updates rather than staying silent
11. If the task runs long enough, confirm Hub sends only quiet periodic heartbeat updates instead of duplicate spam
12. Send `/status` during active or paused work and confirm the current run details include phase, latest progress summary, and last specialist activity
13. If you intentionally pause progress long enough, confirm `/status` reports stale progress honestly
14. Send `/last` after a completed or failed run and confirm the last run details are shown
15. If a long-running task is active, send `/stop` and confirm the run moves to `cancelled`
16. Send `/learn test learning from telegram`
17. Send `/memory` and confirm the learning appears with a stable identifier
18. Send `/forget <identifier>` and confirm the learning is removed

## Evidence To Capture

Capture enough evidence to prove the real flow without exposing secrets:

- terminal output from `uv run agent-hub telegram --debug`
- Telegram screenshots or copied replies for:
  - `/agents`
  - routing to `AI Tech Lead`
  - initial run acknowledgement
  - streamed phase updates
  - quiet heartbeat behavior if exercised
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
- Telegram sends an immediate acknowledgement when the routed task starts
- live progress updates appear during specialist execution without duplicate spam
- clarification and approval behavior works if requested by the specialist
- `/approve` resumes the paused task
- `/reject` safely closes the paused task
- `/status` and `/last` return persisted run information, including current phase,
  latest progress summary, and last specialist activity where applicable
- stale progress is reported honestly if specialist activity stops while the run is still alive
- `/stop` cancels active work without silently converting it to `failed`
- `/learn`, `/memory`, and `/forget` work against the Hub knowledge store
- task lifecycle is persisted in `data/task_runs.sqlite3`
- token usage and estimated cost are recorded in `data/llm_usage.json`
- logs remain human-readable and do not expose secrets

## Validation Notes

Record the final evidence location and outcome in the live backlog row for `AGENT-HUB-015`.
