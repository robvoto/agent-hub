# Hub Routing — Interaction Diagram

This is the interaction diagram for how a request reaches the right agent.
The orchestrator receives every request and decides which agent handles it.
You never route manually unless you want to.

```mermaid
sequenceDiagram
    actor You
    participant Bot as Hub Telegram Bot
    participant Orch as Orchestrator LLM<br/>(create_react_agent)
    participant Mem as Hub Memory<br/>(hub_memory.py)
    participant Reg as Agent Registry<br/>(config/agents/)
    participant ATL as AI Tech Lead<br/>subprocess
    participant FB as Factory Brain<br/>(factory_bridge, resumable thread)

    You->>Bot: "Fix the null pointer bug"
    Bot->>Orch: forward message

    Note over Orch,Mem: System prompt is rebuilt every turn,<br/>folding in active stored learnings<br/>(operator /learn facts ranked ahead<br/>of auto-inferred ones)
    Orch->>Mem: format_learnings_for_prompt()
    Mem-->>Orch: learnings block

    Orch->>Reg: list available agents
    Reg-->>Orch: [ai-tech-lead, ...]

    Orch->>Orch: LLM decides: coding task -> ai-tech-lead

    Orch->>ATL: run-agent-task input.json

    alt Needs clarification
        ATL-->>Orch: status: needs_clarification
        Orch-->>Bot: relay question
        Bot-->>You: "Which null pointer bug?"
        You->>Bot: "The result parser"
        Orch->>ATL: run-agent-task (updated input)
    end

    ATL-->>Orch: status: success, instruction ready
    Orch-->>Bot: formatted response
    Bot-->>You: summary + instruction

    Note over You, FB: Design request

    You->>Bot: "Design me an agent for X"
    Bot->>Orch: forward message
    Orch->>Orch: LLM decides: factory/design -> factory brain
    Orch->>FB: invoke_factory_request(thread_id)

    alt Approval required
        FB-->>Orch: interrupted, approval_token
        Orch-->>Bot: "Factory Brain drafted a spec. /approve to stage it."
        Bot-->>You: spec summary
        You->>Bot: /approve
        Bot->>Orch: approval
        Orch->>FB: resume_factory_request(thread_id)
        FB-->>Orch: status: success
    end

    Orch-->>Bot: formatted response
    Bot-->>You: result

    Note over You, Mem: Explicit learning (bypasses the orchestrator LLM entirely)

    You->>Bot: "/learn Prefer Telegram for operator control"
    Bot->>Mem: orchestrator.learn(value, source)
    Note over Mem: Stored immediately as an active,<br/>operator-scoped record — no approval<br/>gate, never auto-superseded
    Mem-->>Bot: confirmation
    Bot-->>You: "Stored learning mem-xxxxxxxx"
```

## How the LLM decides which agent to call

Each enabled agent in `config/agents/` (plus Factory Brain, wired in directly) becomes a tool in the orchestrator's tool list:

```
Tool: ai-tech-lead
Description: "AI Tech Lead: Handles coding task clarification, risk review,
              planning, and instruction generation for coding backends."

Tool: factory-brain
Description: "Specialist agent for creating, configuring, validating,
              and staging agents."
```

The orchestrator LLM reads your message and picks the right tool. If no agent fits, it answers directly or says what's missing.

## Memory: automatic vs. explicit

`/learn`, `/memory`, `/forget`, and `/learn-mode` are intercepted as commands before the
message ever reaches the orchestrator LLM — `HubOrchestrator.learn()` writes directly
to storage with no approval gate. Separately, on every LLM turn `_build_system_prompt`
re-reads the store and folds active learnings into the system prompt, with
operator-established (`/learn`) records always ranked ahead of auto-inferred ones so a
newer background guess can never crowd out an older explicit instruction. See
`src/agent_hub/hub_memory.py` for the full operator/auto scope model.
