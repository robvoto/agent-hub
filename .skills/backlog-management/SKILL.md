---
name: backlog-management
description: Use ONLY for backlog work — Google Sheet rows, IDs, priorities, duplicates, implementation state, evidence, human review flags. Do NOT use for implementing the underlying code (see hub-runtime-change) except to update backlog evidence once work is done.
---

# Skill: Backlog Management

Use when creating, updating, deduplicating, grooming, or analysing backlog items.

## Source of truth

- Working backlog: `https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178`
- Spreadsheet ID: `1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA`
- This Google Sheet is the only backlog source of truth for Agent Hub. Do not create local backlog files as a substitute.
- Read the header row first and update by column name, never by fixed position.
- Do not add, remove, or rename columns unless explicitly agreed.

## How to read and write the sheet

Load `.skills/human-mcp-access/SKILL.md` first.

Preferred access is the secure-first Human MCP configuration from `.mcp.json`, following `.skills/human-mcp-access/SKILL.md`. Use its ngrok fallback only after a genuine secure failure, stated explicitly. Runtime connector names may differ, so inspect the current tool catalogue rather than guessing. Use:

- `sheets_read_rows` to inspect the live header and all existing rows
- `sheets_append_row` to add a complete row
- `sheets_update_cell` to update an existing row by freshly resolved 1-based row and column

After every write, re-read the live sheet and verify the result before claiming completion.

### Required access rule

If no authorised tool can read **and write** the live Google Sheet, or a write fails:
- Stop backlog work.
- Do not claim the sheet was updated.
- Do not fake it by creating a copy, a new file, or a local export as a substitute.
- Report the blocker plainly and hand the human ready-to-paste rows instead, in the exact column order below.
- (A Drive connector that can only read/create-new-file, not edit an existing file's rows, counts as "no write tool" for this purpose.)

## Column order (as of 2026-07-22)

ID, Creator, Title, Epic, Type, Priority, Size, Problem, Outcome, Acceptance Criteria, Original Source, Duplicate Of, Depends On, Notes, Implementation State, Implementation Date, Implemented By, Evidence, Human Review Needed, Review Category, Review Reason, Created Date, Modified Date, Resolved Date

## ID prefixes in use

Multiple prefixes coexist by era/theme — don't force one global counter:
- Legacy pre-rename IDs already present in the live sheet remain immutable; do not create new rows with retired prefixes.
- `HUB-MVP-###`: MVP feature work
- `HUB-LEARN-###`: typed-memory/learning system work
- New theme: pick a short, descriptive prefix (e.g. `HUB-DISPATCH-###`) and continue it for related items rather than inventing a new prefix per row.

To find the next number for a prefix: read existing IDs with that prefix, take the highest, increment by 1.

## Implementation State values (as actually used)

- `Implemented`: code/tests meet the acceptance criteria — evidence must name files/functions/tests.
- `Planned`: not implemented yet.
- `Blocked`: implementation attempted but blocked on something external (missing credentials, missing decision) — say what's blocking it in Notes/Evidence.

Do not write vague states like "needs check" into Implementation State — use `Human Review Needed` + `Review Category` + `Review Reason` for that instead.

## Creating a row from a rough finding or finished task

Fill what's knowable rather than asking the human to fill every field:
- `ID`: next number for the relevant prefix (see above).
- `Creator`: `Rob` if the human raised it; leave blank or name the agent if the agent found it while working.
- `Title`: concise action phrase.
- `Problem` / `Outcome`: why it matters / what success looks like.
- `Original Source`: "Conversation with Rob, <date>" or the file/code area that surfaced it.
- `Depends On`: existing IDs that must land first.
- `Implementation State`, `Implementation Date`, `Implemented By`, `Evidence`: only fill when verified — evidence names files/functions/tests, not "looks done."
- `Human Review Needed` = yes + `Review Category`/`Review Reason` whenever something is implemented at the code/test level but not yet proven with a real live run (a live Telegram flow, a real specialist subprocess, a real cross-project dispatch). This project has a recurring pattern of exactly that gap — don't skip flagging it.

## Grooming existing rows

- Do not delete rows without the human's agreement.
- Prefer marking duplicates with `Duplicate Of` plus a note, over silently merging or removing.
- If unsure whether a row is stale/duplicate/wrong, use the review columns instead of rewriting it as fact.

## Checklist

- Did you confirm write access before claiming the sheet was updated?
- Does the row use an existing prefix where the work is a continuation, not a new one per item?
- Is `Human Review Needed` set for anything only validated by tests, not a live run?
- Is evidence a specific file/function/test, not a vague claim?

## Finish format

Report:
- Row(s) created or updated (or: blocked, with the exact rows drafted for manual paste-in)
- Any duplicates suspected
- Any Implementation State changes and their evidence
