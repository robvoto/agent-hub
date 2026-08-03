# Security Policy

## Sensitive material

Agent Hub may handle project paths, task instructions, approval state, specialist outputs, Telegram metadata, model-provider configuration, and execution logs. Do not commit or publish:

- API keys, bot tokens, cookies, passwords, or webhook secrets;
- local `.env` files or credential stores;
- private project source copied from managed repositories;
- raw task logs containing personal or confidential information;
- resume tokens, approval tokens, or internal connector credentials;
- generated state databases or operator-specific configuration.

## Agent execution

- Invoke specialists only through registered contracts.
- Keep project identity pinned for the lifetime of a task.
- Treat specialist output as untrusted until validated.
- Require explicit approval for actions defined as consequential by the active contract.
- Do not expand file, repository, command, or network scope beyond the approved task.
- Preserve failures and validation results instead of reporting unverified success.

## Telegram and external channels

- Store bot credentials outside source control.
- Restrict bot access to approved users or chats.
- Do not send secrets, private source files, or full sensitive logs through chat.
- Treat incoming chat text as untrusted instructions subject to the same approval and scope controls as CLI requests.

## Logs and persistence

Logs should support diagnosis without becoming a second copy of private projects or credentials. Redact secrets and minimise retained payloads. Runtime state must be stored in ignored local paths with access limited to the operator.

## Reporting

Report suspected vulnerabilities privately to the repository owner. Use a sanitised reproduction and never attach live credentials, private repository contents, or operator databases to an issue.
