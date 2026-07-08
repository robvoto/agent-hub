# Agent Hub

Main orchestrator, runtime, and control plane for the local agent platform.

Agent Hub is the entry point for operator interaction. It receives work from CLI or Telegram, manages runtime orchestration, and routes tasks to specialist agents.

## Repo role

| Repo | Responsibility |
|------|----------------|
| `agent-hub` | Runs and controls agent work |
| `agent-factory` | Creates, configures, and stages agents |
| `ai-tech-lead` | Specialist coding/technical-lead agent |

Agent Hub does not create or stage agents. That belongs to Agent Factory.

## Canonical runtime path

Run this repo from WSL:

```text
~/projects/agent-hub
```

Do not use `E:\Programming` as the canonical runtime path.

## Documentation

Start with `docs/INDEX.md`. It is the single documentation entry point.

## Backlog

The live backlog and single source of truth for Agent Hub is the Google Sheet:

https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178

Do not create duplicate local backlog files unless explicitly requested.
