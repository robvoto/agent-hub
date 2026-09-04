---
name: human-mcp-access
description: Use when Agent Hub work requires Human MCP, especially Google Sheets or Google Docs read/write access. Load this before backlog-management when the live Agent Hub backlog must be inspected or changed.
---

# Skill: Human MCP Access

Use this skill to discover and call the repo-configured Human MCP connection without guessing or substituting a local file.

## Canonical configuration

- MCP config: `.mcp.json`
- Primary server: `human-mcp-secure` at `http://127.0.0.1:8001/mcp`
- Fallback server: `human-mcp-ngrok-fallback` at the deliberate ngrok endpoint in `.mcp.json`
- Use secure first. Use the ngrok fallback only after a genuine secure failure, and state the secure failure before switching.
- Runtime connector names may differ from these configuration names. Inspect the current client tool catalogue before calling a tool; do not guess a connector name.
- Read the URLs from `.mcp.json`; do not duplicate, silently replace, or use an old server name elsewhere.

## Agent Hub backlog

- Spreadsheet ID: `1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA`
- Sheet/tab name: `Backlog`
- Browser gid: `32071178`
- The live Google Sheet is the only backlog source of truth.

## Preferred MCP tools

Use the secure Human MCP tools directly when they are exposed by the current client:

- `sheets_read_rows`: read the complete tab, including its header.
- `sheets_append_row`: append one complete row.
- `sheets_update_cell`: update a specific existing field after locating the live row and column.
- `docs_read_text`, `docs_append_text`, `docs_replace_text`: Google Docs operations when explicitly needed.

## Backlog access workflow

1. Discover the current runtime tools and select the secure Human MCP connector.
2. Call `sheets_read_rows` for the Agent Hub spreadsheet and `Backlog` tab.
3. Read the live header and map columns by name, never by assumed position.
4. Check existing IDs, titles, problems, dependencies, and implementation evidence before creating anything.
5. Deduplicate against existing rows. Do not invent a new ID until the live sheet has been checked.
6. Append or update only the agreed fields.
7. Re-read the sheet and verify the written row or cells before claiming success.
8. Follow `.agents/skills/backlog-management/SKILL.md` for backlog content and governance rules.

## When MCP tools are not exposed directly

Do not conclude that Sheets access is unavailable until the secure configuration and actual tool catalogue have been checked.

When the current environment exposes only local project command execution:

1. Use the secure URL from `.mcp.json`.
2. Use MCP Streamable HTTP JSON-RPC against that URL.
3. Send `initialize` using protocol version `2025-03-26`.
4. Preserve the returned `Mcp-Session-Id` header.
5. Send `notifications/initialized`.
6. Call `tools/list` and confirm the required Sheets tool is advertised.
7. Call `tools/call` with the exact tool name and arguments.

If secure initialization, discovery, or a required call genuinely fails, report that exact failure, then repeat this bounded workflow against `human-mcp-ngrok-fallback`. Never silently switch and never use a local or stale backlog copy.

Required call arguments:

```text
sheets_read_rows:
  spreadsheet_id
  sheet_name

sheets_append_row:
  spreadsheet_id
  sheet_name
  values

sheets_update_cell:
  spreadsheet_id
  sheet_name
  row
  col
  value
```

The `row` and `col` values are 1-based. For updates, derive both from the freshly read sheet.

## Safety and stop conditions

- Never print tokens, credentials, cookies, or unrelated MCP configuration values.
- Never use a local Markdown, CSV, Excel file, or copied spreadsheet as a substitute for the live backlog.
- Never claim a write succeeded without re-reading the live sheet.
- Never create backlog IDs from memory alone.
- If secure and, where appropriate, the declared fallback both fail, stop and report the exact blocker.
- Do not fall back to web scraping or a read-only Drive connector for backlog writes.
