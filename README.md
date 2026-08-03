# Agent Hub

Agent Hub is the local control plane for a multi-agent delivery platform. It receives work from human-facing channels, resolves project context, applies approval boundaries, and routes bounded tasks to registered specialist agents.

## Platform role

```text
Human / CLI / Telegram
          │
          ▼
      Agent Hub
          │
          ├── Agent Factory
          │     Creates and stages agent packages
          │
          └── AI Tech Lead
                Plans and coordinates bounded coding work
```

Agent Hub is the operator entry point. It does not own specialist implementation logic and it does not create agents directly.

## Responsibilities

- accept and classify incoming work;
- resolve the selected project and pin that context to the task;
- route work through registered agent contracts;
- preserve approval, clarification, and resume state;
- record execution outcomes and operational events;
- prevent a task from silently switching projects during execution;
- provide a consistent CLI and Telegram-facing control surface.

## Repository boundaries

| Repository | Responsibility |
|---|---|
| `agent-hub` | Orchestration, routing, task state, approvals, and operator interaction |
| `agent-factory` | Agent package creation, validation, staging, approval, and promotion |
| `ai-tech-lead-agent` | Technical planning and bounded coding-agent coordination |

Changes that belong to a specialist should remain in that specialist repository rather than being duplicated in Agent Hub.

## Architecture principles

- **Pinned project context** — a task keeps the project identity it started with.
- **Explicit contracts** — specialists are invoked through versioned machine-readable boundaries.
- **Human approval** — risky or consequential actions pause instead of proceeding silently.
- **Resumable work** — clarification and approval interruptions preserve enough state to continue safely.
- **Observable execution** — outcomes, failures, approvals, and rework should be inspectable.
- **Local-first operation** — credentials, runtime state, and project files remain under operator control.

## Documentation

Start with [`docs/INDEX.md`](docs/INDEX.md). It is the canonical documentation entry point for architecture, runtime, commands, contracts, and operational guidance.

## Backlog

The live backlog and single source of truth for Agent Hub is the [Agent Hub Google Sheet](https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178).

Do not create duplicate local backlog files unless explicitly requested. Repository documentation should describe product behaviour and architecture rather than duplicate mutable backlog rows.

## Development status

Active private project. Interfaces between Agent Hub, Agent Factory, and AI Tech Lead are still evolving and should be treated as explicit contracts rather than inferred from implementation details.

## Security

See [`SECURITY.md`](SECURITY.md) for secrets, Telegram, subprocess, project-path, and log-handling boundaries.

## Licence

This private repository does not grant an open-source licence. A licence should be selected deliberately before any public source release.
