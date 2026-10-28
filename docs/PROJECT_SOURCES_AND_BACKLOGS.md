# Agent programme — authoritative plans, handoffs and backlogs

**Purpose:** Navigation only; never duplicate another project's master plan or handoff here. Each project owns its code, current documentation, decisions and task handoff in its own repository. Google Sheets remain authoritative only where an actual project backlog exists there. Updated 28 October 2026. Links below are grounded in currently inspected repository documentation or previously verified project links.

| Programme | Current project documentation / handoff | Backlog / task register |
|---|---|---|
| Agent Factory | [Factory documentation index](https://github.com/robvoto/agent-factory/blob/main/docs/INDEX.md) | [Google Sheets Factory backlog](https://docs.google.com/spreadsheets/d/1outLuOWhd-A7uvzsl9C2Jc-tKpsyci9HPZalxcmFiOg/edit?gid=1912613036#gid=1912613036) |
| Agent Hub | [Hub documentation index](INDEX.md) and [architecture](ARCHITECTURE.md) | [Google Sheets Hub backlog](https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178) |
| AI Tech Lead | [ATL documentation index](https://github.com/robvoto/ai-tech-lead-agent/blob/main/docs/INDEX.md) | [Google Sheets ATL backlog](https://docs.google.com/spreadsheets/d/1-e2lQ6vLUD8A5t3cuLhrjRvdTbs3hfs4ptdEE2yDaEc/edit) |
| Affordability adviser | [Current engineering plan and handoff](https://github.com/robvoto/household-budget/blob/main/docs/AFFORDABILITY_ENGINE.md) | [GitHub Affordability issues](https://github.com/robvoto/household-budget/issues) (do not invent a Google Sheets backlog) |
| Shared AWS Telegram/AgentCore platform | [AWS platform documentation index](https://github.com/robvoto/aws-agent-platform/blob/main/docs/INDEX.md) and [Harness guide](https://github.com/robvoto/aws-agent-platform/blob/main/docs/AGENTCORE_HARNESS_ARCHITECTURE.md) | [Platform GitHub issues](https://github.com/robvoto/aws-agent-platform/issues) |
| MCP-server / shared platform infrastructure | [Master infrastructure plan](https://github.com/robvoto/MCP-server/blob/main/docs/AGENT_INFRASTRUCTURE_MASTER.md) and [MCP repository](https://github.com/robvoto/MCP-server) | [MCP GitHub issues](https://github.com/robvoto/MCP-server/issues); no separate verified Google Sheets backlog |
| Job Hunter | [Documentation index](https://github.com/robvoto/job-hunter-agent/blob/main/docs/INDEX.md) and [AWS retirement/recovery runbook](https://github.com/robvoto/job-hunter-agent/blob/main/docs/aws-ec2-setup.md) | [Job Hunter Google Sheets backlog](https://docs.google.com/spreadsheets/d/1-D7RzYB3R39dOmUFZvvsDlWpDfIVn3eRajEae9b7OX0/edit?gid=218702820#gid=218702820) |
| Job Market Map | [Project repository](https://github.com/robvoto/job-market-map) (verify current document index before linking a particular handoff) | Not yet verified — inspect source before claiming a Google backlog |
| Adaptive Teaching Agent | [Design proposal owned by Hub](ADAPTIVE_TEACHING_AGENT_PLAN.md); specialist is not implemented | Not yet assigned a verified task row |
| OpenCode configuration/approvals | MCP and OpenCode owner-specific configuration and permission evidence should be maintained in the relevant configuration and MCP repository, not in arbitrary agent prompts | No independently verified backlog |

## Migration rule

- **Library is not the canonical location for coding-agent plans, source-of-truth handoffs or project skills.** Where a Library item contains unique useful decisions, integrate it into the existing owner project's document or appropriate skill, verify and then remove the duplicate Library copy. Do not automatically import old project status as current.
- Keep original private backups, bank evidence and credentials outside public Git. A recovery runbook may describe their existence without copying private bytes.
- The Library root index remains a locator until project references are updated. Move/delete Library originals only after content and version checks; do not mass-delete on a filename match.
- MCP-server's project checkout was not present under `~/projects/MCP-server` in the current WSL enumeration. The verified repository links are given above; locate and inspect the authorised working tree before modifying its files.
- This table is navigation, not an implementation-status claim or a substitute for the project-specific backlog.
