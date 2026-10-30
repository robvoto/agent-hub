# Agent Hub — Vision, Architecture & Implementation Plan

> **Canonical Document Reference:** Single source of truth for Agent Hub product architecture, direct inter-agent collaboration, memory persistence, asynchronous workflow orchestration, and communication integrations.

---

## 1. Executive Vision
Agent Hub is the central conversational coordinator. The user interacts with Hub naturally. Hub understands intent, selects from a growing pool of specialised independent agents, coordinates cross-agent workflows, and remembers context across weeks and months.

**Key Metaphor:** Agents are high-power, autonomous "super-skills" packed with their own tools, deterministic guardrails, and validation logic, orchestrated by Hub rather than siloed into isolated chats or reduced to simple 1-shot tools.

---

## 2. Direct Inter-Agent Collaboration & Cross-Worker Bridge

Agents and AI workers must collaborate directly without Rob acting as a copy-paste intermediary.

```
[ Rob requests complex task in Telegram ]
                   │
                   ▼
       [ Agent Hub Orchestrator ]
                   │
    ┌──────────────┴──────────────┐
    ▼                             ▼
[ AWS Specialist Agent ]      [ Codex / OpenCode CLI Worker ]
(Cloud deal scraping, DB)      (Deep code gen, heavy refactoring)
    │                             ▲
    │  Direct Work Handshake       │
    └─── (JSON Subprocess / SQS) ──┘
         Exchanges tasks, findings,
         and follow-up questions
```

### Supported & Bounded Inter-Agent Transports:
1. **AWS Lambdas ⟷ Local CLI Workers (Codex / OpenCode / Antigravity):**
   - *Supported Route:* SQS Queue or direct local JSON subprocess execution via the controlled task runner.
   - *Limitation:* Web chat sessions (ChatGPT, Claude.ai, Gemini Web) **do not have server-to-server APIs** to talk directly to each other; all automated collaboration routes via CLI, SDK, or official Bedrock/OpenAI APIs.
2. **Telegram Bot-to-Bot Communication (Investigation & Corrections):**
   - *Official API Support:* Telegram supports bot-to-bot communication under specific conditions (such as Telegram Business bots, Bot API 7.0+ business connections, or specific administrator rights per [Telegram Bot-to-Bot Docs](https://core.telegram.org/api/bots/bot-to-bot)).
   - *Standard Bots Constraint:* In standard non-business group/private chats, Telegram filters standard bot messages to prevent infinite echo loops.
   - *Architecture Route:* Where supported (Business connections or webhooks), direct bot-to-bot messaging can be evaluated. In standard chats, Hub's internal SQS/HTTP task dispatch provides a reliable asynchronous transport without relying on Telegram's filtering rules.
3. **Safety & Loop Protection Guardrails:**
   - **Recursion Ceilings:** Hard limit of max 3 follow-up question loops between workers on any single task.
   - **Cost & Token Ceilings:** Every cross-agent collaboration inherits the parent task's `$0.10` spend cap and max-token budget.
   - **Human Checkpoints:** Any consequential action (purchases, code merges, destructive edits) halts for Rob's explicit approval.

---

## 3. High-Priority Communication Assistance: Email & WhatsApp

> [!IMPORTANT]
> Assisting Rob with **Email (Gmail)** and **WhatsApp** is a higher product priority than managing Telegram messages.

- **Capabilities:**
  1. *Discovery & Triage:* Read, search, and understand incoming emails and WhatsApp messages.
  2. *Drafting Responses:* Prepare high-quality suggested replies grounded in Rob's knowledge and personal context.
  3. *Approval Gate (Non-Negotiable):* **NEVER send an email or WhatsApp message automatically.** All outgoing communications are presented as drafts in Telegram or UI for Rob's explicit one-click confirmation (`/approve` or button tap).
- **Tooling:**
  - Gmail: Native Google Workspace tools / MCP integration using Rob's authorised service account.
  - WhatsApp: Human MCP browser integration or WhatsApp Business API adapter with explicit draft preview.

---

## 4. Multi-Conversation & Telegram Topics (Threaded Mode)

- **Correction & Implementation on Telegram Topics:**
  - Rob enabled **Threaded Mode (Direct Topics)** for the AWS bot.
  - Telegram supports message threads directly in private 1-on-1 bot chats without needing a public group. (Note: Telegram Web K lacks supergroup conversion and topic toggles for basic groups; bot threaded mode or Telegram Desktop/mobile should be used for group topic management).
  - **AWS Implementation (Implemented & Fully Tested):**
    1. `telegram_update.py` structurally extracts and validates `message_thread_id` and supports negative group chat IDs.
    2. `ingress.py` sets SQS `MessageGroupId=f"{chat_id}_{message_thread_id}"` for per-topic FIFO isolation.
    3. `telegram_client.py` sends outbound replies targeted directly to `message_thread_id` across both private and group chats.
    4. `factory.py` provides `resolve_session_id` (`{chat_id}:topic_{message_thread_id}`), durable `hub_conversation` history, and unified session cleanup on `/new` or `/reset`.
    5. **Amnesia Resolution:** `worker.py` persists recent dialogue in `hub_conversation` and injects conversation context into `route_request` (resolving follow-ups like *"under A$20"* from prior fire-station dialogue) and passes `conversation_history` down into specialist Lambda envelopes and local execution.
    6. **Topic Isolation Across ALL Specialists:** Every platform specialist (`shopping.py`, `bank_tax.py`, `ai_tech_lead.py`, `career.py`, `travel.py`, `trading.py`, `factory.py`) derives its DynamoDB session key via `resolve_session_id`, ensuring two parallel topics using the same specialist never collide. Automated test suite (`test_topics_and_amnesia.py`) verifies isolation and context flow.

### The 3-Layer Memory Architecture:
1. **Layer 1: Immediate Buffer (Last 3–5 turns):** For fast follow-ups (*"under $20"*, *"yes, proceed"*).
2. **Layer 2: Living Conversation Brief (1-page summary per topic):** Automatically updated in the background. Tracks your active goals, decisions made, and where you left off.
3. **Layer 3: Cross-Chat RAG (Semantic Memory):** Key facts and milestones are indexed across all topics so relevant context from one topic can be discovered in another without token bloat.

---

## 5. Asynchronous Multi-Agent Workflows & Task Lifecycle

Work does not always finish in a single message turn. Hub orchestrates real work across specialists using an event-driven task model:

```
[ Hub Orchestrator (Parent Run) ]
             │
             ├── Dispatches Async ──► [ Specialist Child Run 1: Shopping ] ──► Emits Progress Events
             │                                   │
             │                                   ▼ (Yields structured deals + pricing)
             │                        [ Handoff Validation & Join Gate ]
             │                                   ▲
             │                                   │ (Checks budget against $15k target)
             └── Dispatches Async ──► [ Specialist Child Run 2: Affordability ]
             │
             ▼
[ Checkpoint: Awaiting Approval if >$100 or policy threshold ]
             │
             ▼ (Resumes on user confirmation)
[ Verified Synthesis delivered to Telegram Topic ]
```

### Core Lifecycle Rules:
1. **Parent Runs & Child Runs:** Hub persists a durable Parent Run in DynamoDB (`task_runs`). Independent specialists run as Child Runs with their own state, logs, and evidence.
2. **Decoupled Asynchronous Execution (Minutes to Hours):**
   - API Gateway / Ingress returns immediately (`202 Accepted` / instant Telegram receipt). It **never blocks** waiting for long-running work.
   - Tasks taking up to 15 minutes run in background SQS Lambda workers; multi-hour jobs run on local PC workers via Human MCP.
3. **Resumable Checkpoints (Human-in-the-Loop):**
   - Pauses for approval (`PAUSED_FOR_APPROVAL`) preserve state indefinitely.
4. **Structured Handoff Validation & Joins:**
   - Hub validates child outputs against frozen schemas before joining them into a final recommendation.

---

## 6. Implementation Roadmap

### Phase 1: Context-Aware Conversational Hub & Threaded Mode Support
- **Goal:** Support Telegram private topics (`message_thread_id`), eliminate amnesia on follow-ups, and ensure conversational history is passed to Bedrock.

### Phase 2: Asynchronous Multi-Agent Coordinator & Direct Worker Handoffs
- **Goal:** Enable Hub to coordinate parallel and dependent specialist child runs, track progress, support pause/resume approval checkpoints, and bridge CLI workers (Codex/OpenCode).

### Phase 3: Email (Gmail) & WhatsApp Assistance Engine
- **Goal:** Search, understand, and draft email and WhatsApp responses with strict human-in-the-loop approval before delivery.

### Phase 4: 3-Layer Memory & Cross-Topic Semantic RAG
- **Goal:** Enable living briefs and cross-topic knowledge retrieval.

### Phase 5: Least-Privilege IAM Hardening
- **Goal:** Replace broad `AdministratorAccess` on `agent-platform-runner` with a scoped, least-privilege IAM policy.

---

## 7. Canonical Backlog Status & Sync

All roadmap items are synchronized with the single source of truth in Google Drive:
- **Canonical Spreadsheet:** `00_Master_Roadmap_and_Decisions` (`1mj1YvYyhwvOZWakS1G-2dk-E0LLG7aOFHc64CgTlrmw`)
- **Direct Sheet Link:** [00_Master_Roadmap_and_Decisions](https://docs.google.com/spreadsheets/d/1mj1YvYyhwvOZWakS1G-2dk-E0LLG7aOFHc64CgTlrmw/edit)

### Synchronized Entries (Sheet1):
| ID | Title | Domain | Status | Priority | Notes |
|---|---|---|---|---|---|
| **HUB-003** | Telegram Topic Threads & Multi-Turn Amnesia Resolution | `aws-agent-platform` | `DONE` | `P0` | Resolved session state partitioning by `chat_id:message_thread_id` across all specialists + durable `hub_conversation` memory in DynamoDB. Negative group IDs supported. |
| **HUB-004** | Cross-Agent & AI Worker Autonomous Collaboration Bridge | `aws-agent-platform` | `BACKLOG` | `P1` | Enables direct machine-to-machine task exchange between AWS Agent Platform, Codex, and Claude/Gemini workers without manual chat copy-pasting. Governed by US$0.10 task cap and human approvals. |
| **COMM-001** | Email (Gmail) & WhatsApp Assistance with Approval Gate | `shared-tools` | `BACKLOG` | `P1` | Assist finding, understanding, and drafting responses to Gmail (Google Workspace) and WhatsApp with mandatory human approval before sending. Priority over Telegram message management. |
| **TRADE-001** | Daily Trading Decision Specialist & Human-MCP Staged Execution | `trading-agent` | `DONE` | `P1` | Daily 8:30 AM Sydney decision scan, blue-chip mean reversion, multi-model critic, topic isolation, and staged trade tickets without fake pricing. |
| **CAREER-001** | Career & Professional Development Specialist Agent | `career-agent` | `DONE` | `P1` | Isolated CareerAgentFunction microservice + Virgin 30-60-90 onboarding + STAR story simulator + CV tailoring + LinkedIn audit tools. |
