# OpenCode completion notifications (opt-in)

How the Hub can forward OpenCode task outcomes to the operator over the existing
Telegram channel, and the single step still required to make it live.

## What exists

`src/agent_hub/opencode_notify.py` is an **opt-in, disabled-by-default** bridge.
When enabled it reads the OpenCode completion supervisor's SQLite `events`
outbox (read-only) and forwards each new event through the Hub's existing
Telegram gateway (`TelegramGateway.notify_operator` → `_send_message`), to the
already-configured primary chat. No second bot, no inbound HTTP port, no
credential duplication.

The supervisor itself lives in the separate `MCP-server-opencode-adapter`
repository and is not deployed; it runs as a bounded manual command today.

## Trust boundary

- The outbox is a **local SQLite file on the same host**, opened `mode=ro`. The
  Hub never writes it.
- Its contents are **untrusted status evidence**. They are rendered as inert
  plain text (`parse_mode=None`, control characters stripped, length bounded)
  and nothing else. A row can never approve or deny a permission, run a command,
  or change Hub policy.
- Notifications are **advisory**, not authenticated evidence. `DONE_REPORTED`
  is a worker self-report; `DONE_VERIFIED` only reflects that the supervisor had
  a separately trusted verifier. The Hub cannot independently confirm either and
  never treats a notification as an approval or a decision.

## Delivery semantics

- **At-least-once**, not exactly-once. An event id is written to an append-only
  ledger only after the send succeeds, so a failed or crashed send is retried.
- **Idempotent consumer**: the ledger is keyed on the outbox event identity
  (`events.dedupe_key`), suppressing duplicates across polls and restarts.
- **Bounded, no starvation**: events are read oldest-first, one batch per poll,
  behind a durable cursor that only advances past delivered or filtered events.
  A send failure stops the cursor so the event is retried; a backlog larger than
  one batch logs a visible warning instead of hiding older events.
- Tolerant of partial writes (truncated ledger/cursor lines are ignored and the
  event is re-delivered) and malformed rows (skipped, logged by short id only).
- No secrets are logged; unexpected evidence keys are never rendered.

## Configuration (all optional; nothing is enabled by default)

| Variable | Purpose |
|---|---|
| `HUB_OPENCODE_NOTIFY_ENABLED` | `1`/`true` to enable the bridge. Absent/other = disabled. |
| `HUB_OPENCODE_OUTBOX_DB` | Path to the supervisor's `supervisor.db`. Required when enabled; startup fails closed without it. |
| `HUB_OPENCODE_NOTIFY_LEDGER` | Ledger file path (default `data/opencode_notify_ledger.jsonl`). |
| `HUB_OPENCODE_NOTIFY_STATES` | Comma-separated state filter (default: all except `RUNNING`/`PENDING`). |
| `HUB_OPENCODE_NOTIFY_MAX_EVENTS` | Events per poll (default 50, max 500). |

## How it starts in-process

`run_telegram()` builds the bridge from the environment and attaches it with
`TelegramGateway.set_notifications`. The gateway polls it once per loop
iteration; a bridge fault is logged and never stops the bot.

## The one missing operational activation step

Nothing runs continuously today, and no live Telegram message has been sent.
The remaining step is a **deployment decision, not new code**:

> Point the bridge at the supervisor's outbox path and (re)start the Telegram
> gateway with `HUB_OPENCODE_NOTIFY_ENABLED=1` and
> `HUB_OPENCODE_OUTBOX_DB=<path>`, while something runs the read-only supervisor
> on a schedule so that path is actually written.

Suggested minimal end-to-end path once authorised:

1. Run the supervisor against the working loopback server (`127.0.0.1:49374`)
   with a fixed `--state-dir` (its `supervisor.db` is the outbox), tracking a
   real session whose worker ends with
   `OC-RESULT task=<id> status=<done|failed|cancelled> ...`.
2. Start the gateway with the two environment variables above so the bridge
   ingests that outbox.
3. Confirm one real operator notification arrives.

Step 1 alone proves the supervisor read path; steps 1–3 together are the first
genuine completion notification. Because this restarts/deploys the live bot, it
needs the operator's explicit authorisation and is deliberately not done here.

## Tests

`tests/test_opencode_notify.py` covers opt-in defaults, fail-closed config,
permission/completion/failed/stalled rendering, advisory wording, malformed and
oversized rows, duplicate/restart/cursor-loss idempotency, transport retry,
no-secret leakage, bounded pagination without starvation, plain-text allow-listed
sends, and non-fatal in-loop polling.
