# Shared Agent Tools — architecture and delivery plan

**Status:** DRAFT / DESIGN ONLY — not approved for implementation  
**Updated:** 17 October 2026  
**Planning owner:** Agent Hub (cross-agent execution); Agent Factory governs specialist capability declarations and agent release approvals  
**Tracked in:** [Agent Hub backlog, AGENT-HUB-060](https://docs.google.com/spreadsheets/d/1v1zJjwGTqhOgb06nYChaGjRNZIVXQht5pNBUbh9r7RA/edit?gid=32071178#gid=32071178)  
**Prior work:** AGENT-HUB-050 (implemented Human MCP gateway), AF-007 (MCP governance), AF-047/054 (manifest/registry and project context)

> Decision boundary: Rob requested a reusable, model-independent tool framework **plan**, not code, a deployed service, a new repository, a signature action or an email send. This document remains a proposal until explicitly approved.

## Outcome and scope

Provide secure reusable tools for specialist agents and approved clients: **Google Drive** (search, read, controlled write), **Gmail** (read, draft, approved send/attachments) and **PDF** (AcroForm and scanned-page filling, preview, validation and authorized signature). Replace repeated one-off scripts and model-dependent connections.

“All agents have tools” means any participating agent may **discover a declared capability and request authorization**. It does *not* mean every agent can automatically read personal files, use Maria's Google account, sign, or send.

Non-goals: replacing Agent Factory's registry, replacing Hub orchestration, building another generic scheduler, duplicating Google APIs per agent, or exposing localhost directly to cloud clients.

## Evidence baseline (checked 17 October 2026)

- Agent Factory publishes and validates agent packages and permission/MCP declarations. Its `config/tools.json` currently lists `run_agent_task`; Factory is not the runtime dispatcher.
- Agent Hub runs LangGraph orchestration, task state, approval/pause/resume and an allowlisted Human MCP gateway for browser, Google Docs/Sheets and read-only Gmail. The Hub does not advertise a native general Google Drive API; Chrome-based browsing is *not* equivalent.
- Existing Hub MCP integration (AGENT-HUB-050) already addresses some approvals. **Reuse and extend only where warranted**, never rebuild it as if absent.
- OpenCode/DeepSeek V4.1 Flash Free independently reviewed the *brief* and returned **REQUEST CHANGES**: distinguish discovery from permission, cloud access from localhost, and native implementations from transport adapters. This was not a separate full source-code audit. [Review session](http://127.0.0.1:4096/server/aHR0cDovLzEyNy4wLjAuMTo0MDk2/session/ses_eb7f1a79affeFJdwBySDQik8fh).
- Existing Hub and Factory worktrees carry unrelated changes. Future work must be isolated from ongoing Shopping tasks.
- The [canonical project standards folder](https://drive.google.com/drive/u/1/folders/14-mkDcSsRd78yTIT17mK03jGPjcGRRNS) was not readable through the current browser identity (access-request screen for thewriter30@gmail.com). **Resolve standards access before changing live platform conventions.** Do not invent its contents.

## Proposed architecture — three separate concerns

| Layer | Proposed ownership | Duties |
| --- | --- | --- |
| Capability contracts / governance | Agent Factory | Stable tool IDs, schema versions, risk tiers, declared scopes, manifest-level allowlists and governed release/rollbacks. Discovery is not authority. |
| Execution broker / policy | Agent Hub runtime boundary, with no specialist import of Hub internals | Enforce caller + user + account + tool + operation authorization; approvals, credential isolation, retries, idempotency, audit, rate limits, output artifact references. One logical enforcement point regardless of adapter. |
| Tool implementations and adapters | Dedicated domain modules behind broker; owned according to scope | One implementation for Drive, one for Gmail, one for PDF. Thin Python client, CLI, MCP and/or HTTP adapters **only if justified**, all enforcing the same policy. |

**Owner direction and current upstream guidance (verified 2026-10-17): native implementation first; MCP is OPTIONAL, not the agent platform.** Build one tested set of Python domain capabilities using the provider's native Google APIs/SDKs and deterministic PDF libraries. Hub/Factory/LangChain agents running inside the trusted local runtime should expose those as ordinary callable/typed tools and preserve Hub's existing approval and audit controls. Do **not** require each agent, specialist or Python call to connect through MCP. LangChain's `create_agent` explicitly accepts ordinary Python callables; MCP adapters are a supported optional interoperability transport, not a reason to copy provider logic. The MCP 2026-07-28 spec warns about token-audience/confused-deputy risks; no MCP token passthrough, unauthenticated localhost proxy or blanket cross-account tool exposure. See [LangChain tools](https://reference.langchain.com/python/langchain/agents/factory/create_agent), [LangChain MCP adapters](https://reference.langchain.com/python/langchain/mcp/tools) and [MCP authorization security](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/authorization/security-considerations.mdx).

**No mandatory standalone broker service.** First extend the existing Hub authorization boundary (AGENT-HUB-050) with a small versioned Python capability package and shared permission checks. A separate broker process/sidecar is only justified if a genuinely distinct authenticated client must cross a process/runtime boundary and tests show a real benefit; record its threat model and lifecycle cost before creating it. Factory should declare and validate permissions, not implement provider APIs or duplicate Hub execution. MCP/CLI/HTTP adapters, if later added, remain thin callers of the same native implementations and policy; deny rules apply equally.

**Deployment is undecided.** PC-on for MVP is approved, but remote/cloud ChatGPT cannot simply call local `127.0.0.1`. Start local read-only capability proof, then verify an authenticated external route only when required. No public tunnel by default.

## Required tool contract (proposal)

For each tool: stable `tool_id`, `version`, JSON input/output schema, read/write/sign/send classification, required caller and account/scopes, approval mode, idempotency behavior, time/file/token limits, typed errors, audit correlation ID, artifact reference types and provider IDs.

Example conceptual call (internal native Python interface or authenticated remote adapter):
`execute(capability_id, version, request_id, validated_inputs, account_ref?, approval_ref?) -> result | needs_approval | denied | failed | unknown_outcome`.

The **authenticated caller principal and actual authorized account come from the trusted runtime**, never from model-provided fields; account and approval references are mere lookup handles verified against server-side authorization. Use the existing draft `docs/schemas/shared-tool-contract-v0.schema.json` rather than inventing a second envelope.

No agent-side raw OAuth token parameters, anonymous account switching, implicit permission expansion, hidden fallbacks or unbounded retries.

## Stage 0 implementation gate — concrete security and cross-runtime evidence (2026-10-17)

**Candidate machine-readable contract (not approved):** [`docs/schemas/shared-tool-contract-v0.schema.json`](schemas/shared-tool-contract-v0.schema.json) now defines `tool.capability`, `tool.request`, and `tool.result` envelopes. It has **no caller-identity fields**, because the broker must inject identity from authenticated transport; account and approval references are never themselves authorization. The schema does not replace per-capability validation, provider OAuth/consent policy or integration tests, and must be independently reviewed before use.



This is a **design-review gate**, not approval to deploy. It follows the coordinator's [six-finding technical review on PR #10](https://github.com/robvoto/agent-hub/pull/10). The earlier independent OpenCode REQUEST CHANGES was a review of the *brief*, not a verified source-level review of this full PR.

1. **Identity proof:** An agent-supplied `caller_id`, `user_id`, `account_id`, or `approval_ref` is untrusted request data. For each supported transport, provide a table of the authenticator and principal (e.g. user-owned local process identity or authenticated client connection), the mapped human/account authorization, revocation, expiry, and failure status. Reject spoofed caller/account/approval IDs and show the test. No anonymous localhost socket may grant access merely because it is on the same PC.
2. **One effective permission intersection:** For every operation, state how the Factory's declared `mcp_servers` and agent permission ceiling intersect with Hub's existing explicit Human MCP gateway allowlist, authenticated broker operation policy, consented account scopes and any per-call approval. **Deny wins.** Tool discovery/annotations are metadata, never evidence of authorization. A Python, CLI or MCP adapter must not bypass this path.
3. **Current credentials and implementation inventory:** Identify the real OAuth/account owner, refresh and scope mechanisms in Human MCP, Hub and provider APIs *before* proposing a second encrypted vault. Record the available Drive, Gmail and PDF implementations and re-use them or justify replacement. Explicitly test that one user's credentials never appear in another agent context or in logs.
4. **External action state machine:** Define bounded approval records over authenticated caller, selected account, exact recipient/destination and content/document hash, operation, expiration and idempotency key; model approved, denied, expired, unknown-provider-result and retry/reconciliation separately. User preference: drafts are reviewed before sending by default, with *bounded* directly instructed human-send exceptions. Never automatically place Maria's or Rob's signature without that signer's specific authorization. A PNG signature is not a cryptographic digital signature.
5. **Real cross-runtime proof:** Stage 1 needs at least **two independently authenticated clients**, one being Hub/approved local agents and the other the intended external ChatGPT client (or an explicit unresolved compatibility blocker). A second call inside one Hub process is not a second runtime. Validate same effective rights, selected mailbox/Drive account, negative denial, audit correlation, credential non-disclosure, timeout and network/PC-unavailable outcomes. Local-only TCP access does not prove cloud ChatGPT connectivity.
6. **Release/rollback evidence:** Before calling Stage 0 approved, attach versioned input/output JSON schema, capability/risk matrix, account/transport permissions table, threat model including compromised worker/forged payload, artifact-retention and revocation policy, tests for all forbidden operations, and a bounded rollback/disable procedure. Obtain an **independent full-source architecture review** plus the operator's approval. Draft PR #10 is not that approval.

**No duplicates:** Existing Hub gateway AGENT-HUB-050, Factory MCP-governance AF-007, the separate AGENT-HUB-061 PDF component, the scoped Drive items AGENT-HUB-063/064 and Gmail backlog AGENT-HUB-062 remain their own workstreams. This plan is the *single architecture decision record*. Do not restart Hub, provision credentials, send email, insert signatures, merge, or deploy as a side effect of editing this draft.

## Security and correctness decisions to prove

1. **Credentials and identities:** Per-user and per-Google-account consent, least-privilege OAuth and OS-backed/encrypted storage. Broker refreshes tokens under concurrency; agents never receive tokens. Keep Rob and Maria separated, with Maria's own authorization.
2. **Sensitive actions:** Read/search and creating unsigned drafts differ from changes, sharing, sending and placing signatures. For consequential actions, bind approval to *exact* signer/account, document hash, recipients/destination, request ID and expiry. Do not accept approval from mere text inside an untrusted document. Require appropriate person's permission for signature use. Signatures inserted as PNGs are **not cryptographic digital signatures**.
3. **Replay, retries and concurrency:** Per-operation persisted idempotency key, deduplication, version/revision preconditions and per-account rate limits. Exactly-once Gmail delivery cannot be guaranteed solely by local idempotency when provider outcome is unknown. In ambiguous cases, inspect provider state and fail closed.
4. **Artifacts:** Preserve original PDFs. Private bounded artifact store, checksum/immutable version, expiring scoped retrieval reference, expiry/retention policy, secure temp-file cleanup, no sensitive bytes in Git, prompts, logs or backlogs. Do not treat ChatGPT Library links as a credential vault.
5. **Policy:** Explicitly denied operations fail closed even via direct Python/CLI or MCP adapter; audit who, account, operation type, approval decision, provider resource ID and bounded outcome without storing credentials or sensitive content.
6. **Untrusted files:** PDFs and email content are untrusted data; limit sizes/pages, detect corruption, sandbox risky processing where feasible, and visually review scanned placement. Automated geometric checks detect some errors, **not all visually incorrect form fills**.
7. **Availability:** Define what happens if PC is asleep, network fails, OAuth expires, user is away, a tool is denied or output validation is uncertain. Never silently change provider, identity or execution route.

## Phased plan and acceptance gates

**0 — Architecture approval and inventory (no code):** Re-read current standards and Hub/Factory runtime, inventory existing Drive/Gmail/PDF connectors and OAuth account routing, identify which capabilities already work; decide ownership, deployment boundary, credentials/artifacts and support matrix. Produce versioned JSON contract and exact security/QA matrix; review with independent agent. Gate: approval of scope and threat model.

**1 — Read-only cross-runtime proof:** Execute one bounded Drive read with a native client and a thin MCP/other adapter, observing identical permission denial, selected account, error and audit semantics. Compare *two real clients*, not two calls inside the same Hub graph. Gate: least privilege and deterministic denial demonstrated.

**2 — Approval and failure semantics:** Prove request-target-bound approve/reject/resume across two clients; mock mutation first. Prove retry/double-request cannot issue duplicate sends, and unknown provider outcomes stop safely. Gate: replay, idempotency and concurrent refresh tests pass.

**3 — Private PDF engine:** Reuse PyMuPDF/pypdf if confirmed suitable; distinguish interactive PDF fields from scanned/nonfillable overlays, support dates and checkboxes, render every page, detect missing/out-of-bounds overlaps, preview and require signer authorization before placing signature. Store verified original/output with artifact IDs. Test two previously troublesome real PDFs, with private samples excluded from source control. Gate: manual visual acceptance and validation report, not a claim of perfect auto-filling.

**4 — Drive and Gmail workflows:** Drive search/read first, controlled update next; Gmail search/draft, then send with exact recipient/attachment approval and provider-state reconciliation. Gate: real multi-account permission tests, approval and error/rollback guidance.

**5 — External access only when required:** Determine if ChatGPT/cloud agent access is genuinely possible via supported connectors or a secure gateway; test auth, expiry, offline availability and revocation. No public tunnel as default. Gate: independently reviewed threat model and live security acceptance.

No build until Stage 0 is reviewed. No new repo until the ownership analysis shows independent engineering requirements. Each stage gets a narrowly scoped Hub backlog item; create an AF companion item **only for actual Factory manifest/governance changes**, not a duplicate cross-project master.

## Open operator decisions — one question at a time

**Q1 — ANSWERED (2026-10-17):** Rob accepts requiring his home PC/WSL to be running for the MVP. Offline-PC/cloud-hosted availability is **not** an MVP requirement. This does **not** establish that cloud ChatGPT can invoke the PC-hosted tool; compatibility and secure routing remain to be proved, and must not be represented as complete.

**Q2 — PARTIALLY ANSWERED (2026-10-17):** Default for email: prepare for human review and obtain approval before sending. **Exception:** a human can explicitly authorize a bounded task to send without a second confirmation; permission is limited to that instruction's recipients, content, account, operation and timeframe. Do not infer an unlimited future-send privilege. **Still open:** whether preparing PDF drafts automatically is always permitted; signature placement authorization policy remains separately subject to signer consent.

Q3: Is Maria's independently authorized account required in MVP, or should we prove Rob's isolated account first?

Q4: Where should private output PDFs live by default: a confirmed private Drive folder or protected local storage?

Q5: What must happen when PC/cloud connector is unavailable: fail with a clear status, or queue for authorized retry under stated limits?

## Current PDF component — independently verified prototype

- Version 0.1.1 standalone Python package remains in ChatGPT Library: `/Workflows/Shared_PDF_Tool_0.1.1_SOURCE.zip` and `/Workflows/shared_pdf_tool-0.1.1-py3-none-any.whl`, indexed by `/LIBRARY_INDEX.md`; previous v0.1.0 moved to `/Workflows/Archive/`. Tracked separately as AGENT-HUB-061. **Migration verification (28 Oct):** both original binaries were recovered and inspected. Source ZIP SHA-256 `8a58d8ac33379a2dcf3c2babee1ef2136a273eee1dd5fdca80e0b0d2e78dd811` (11,963 bytes); wheel SHA-256 `bd939e01faceebe207c6e9faedc650f9ebc9a593f4c9851b211fa59d4d240c40` (8,953 bytes). Source includes `README.md`, `pyproject.toml`, Python package and `tests/test_engine.py`. The archive README calls this a proof of concept, not a shared, deployed execution broker; signatures, real-form acceptance and cloud adapter are not implemented. Original source archive tests were independently run in an isolated extraction on 28 October: `python -m pytest -q tests/test_engine.py` — **7 passed**. This does not imply Hub integration or production acceptance. **Repository byte migration is not yet verified:** the Library/WSL transport blocked transferring the archive to this repo. Keep originals until exact hashes are verified on the destination. Do not claim the PDF tool has been enabled.
- On 17 October 2026 **7 tests passed** after adding post-save exact extraction of text inside explicitly reviewed static overlay rectangles; unsupported glyph loss fails closed. End-to-end original-copy test on a clearly labelled **fictional** two-page transfer form: 27 placements (22 text, 5 checkboxes); SHA256 of original unchanged; no changes outside designated boxes; PyMuPDF and Poppler renderings of all pages manually inspected. This is NOT a verified run on the TPG original, whose raw bytes and linked download remained inaccessible. The component still has no signatures, email send, cloud access, Hub registration, or remote endpoint.
- Current callers: Python code running in a session containing the package and PDF, or a local agent after explicit installation and permission to execute `shared-pdf inspect ...` / `shared-pdf fill ...`. Other ChatGPT chats must first retrieve the indexed package; they cannot assume a universally installed tool.
- TPG is a **regression fixture candidate**, never a special-case customer form or production dependency; verify accessibility and obtain permission to use any private sample. Prior six failures become mandatory acceptance cases.

## Governance / change log

- 2026-10-17: Plan requested, **implementation explicitly deferred**. Independent OpenCode brief review: REQUEST CHANGES. Proposal adds execution broker, transport neutrality, account isolation, artifact boundaries and test gates.
- 2026-10-17 operator decision: MVP may require the home PC to be running. No offline-PC deployment requirement. ChatGPT-to-PC interoperability still unproven; do not silently drop that goal.
- Independent full-source review PASS (2026-10-17) for merge as a design-only proposal; not approval to implement or activate. Pending: project Drive standards access, operator clarification on PDF draft/signature approval, and answers to Q3–Q5, operator approval of actual implementation backlog decomposition.
- This file is the **single proposed cross-agent tool planning document**. Do not create a competing plan; update this and keep AGENT-HUB-060 as its tracking entry.
