# Coding Task — End to End

A full coding task from your message to changed files, through every layer.

```mermaid
flowchart TD
    You([You])

    subgraph Entry["Entry Points - pick one"]
        HubTG["Hub Bot<br/>fix the login bug"]
        ATLTGDirect["AI Tech Lead Bot<br/>/run JH-042"]
    end

    subgraph HubLayer["Hub Layer"]
        HubOrch["Orchestrator LLM<br/>picks a specialist tool"]
        SubProc["Subprocess call<br/>run-agent-task input.json"]
    end

    subgraph ATLWorkflow["AI Tech Lead Workflow (LangGraph, 14 nodes)"]
        ReadReq["1_read_request<br/>validate + normalise"]
        CheckResearch["1b_check_research<br/>local doc evidence enough?"]
        ResearchInterrupt["1c_research_interrupt<br/>ask to fetch online docs"]
        CollectResearch["1d_collect_research_evidence<br/>fetch + cache approved docs"]
        ReviewRisk["2_review_risk<br/>risk level, needs_approval?"]
        TechLead["3c_tech_lead_analyse<br/>formulate task + tech direction"]
        ApprovalInterrupt["4_approval_interrupt<br/>ask human to approve"]
        RequestPlan["5b_request_plan<br/>ask coding agent for a plan"]
        ReviewPlan["5c_review_plan<br/>LLM checks plan matches task"]
        PlanInterrupt["5d_plan_interrupt<br/>ask human after 2 plan rejections"]
        CreateInstruction["5_create_agent_instruction<br/>build bounded, safe prompt"]
        RunAgent["6_run_coding_agent<br/>Codex / Claude Code subprocess"]
        FailureInterrupt["6b_failure_interrupt<br/>ask human after 2 failed retries"]
        EndNode["7_end_node<br/>log outcome, return result"]
    end

    subgraph HumanGates["Human-in-the-Loop Gates - 4 distinct interrupts"]
        ResearchQ["Approve online research?"]
        ApproveQ["Approve this task?"]
        PlanQ["Guide the rejected plan"]
        FailQ["Guide after repeated failure"]
    end

    subgraph Output["Output"]
        JSONOut["output.json<br/>status, instruction,<br/>result, evidence"]
        TGResponse["Telegram response<br/>summary to you"]
        ChangedFiles["Changed files<br/>in target project"]
    end

    You --> HubTG
    You --> ATLTGDirect

    HubTG --> HubOrch
    HubOrch --> SubProc
    SubProc --> ReadReq
    ATLTGDirect --> ReadReq

    ReadReq --> CheckResearch
    CheckResearch -->|complex task, local docs insufficient| ResearchInterrupt
    CheckResearch -->|sufficient or not complex| ReviewRisk

    ResearchInterrupt --> ResearchQ
    ResearchQ -->|your answer| ResearchInterrupt
    ResearchInterrupt -->|approved| CollectResearch
    ResearchInterrupt -->|declined| EndNode
    CollectResearch --> ReviewRisk

    ReviewRisk --> TechLead

    TechLead -->|needs approval| ApprovalInterrupt
    TechLead -->|no approval needed or already approved| RequestPlan
    ApprovalInterrupt --> ApproveQ
    ApproveQ -->|/approve or /reject| ApprovalInterrupt
    ApprovalInterrupt -->|approved| RequestPlan
    ApprovalInterrupt -->|rejected| EndNode

    RequestPlan --> ReviewPlan
    ReviewPlan -->|approved| CreateInstruction
    ReviewPlan -->|rejected, retries under 2| RequestPlan
    ReviewPlan -->|rejected twice, or reviewer unavailable| PlanInterrupt
    PlanInterrupt --> PlanQ
    PlanQ -->|your guidance| PlanInterrupt
    PlanInterrupt --> RequestPlan

    CreateInstruction --> RunAgent
    RunAgent -->|success| EndNode
    RunAgent -->|failure, retries under 2| CreateInstruction
    RunAgent -->|failure, retries at 2| FailureInterrupt
    FailureInterrupt --> FailQ
    FailQ -->|your guidance| FailureInterrupt
    FailureInterrupt --> CreateInstruction

    EndNode --> JSONOut
    JSONOut --> TGResponse
    JSONOut --> ChangedFiles
    TGResponse --> You

    style ResearchQ fill:#fff3e0,stroke:#e6a817
    style ApproveQ fill:#fce8e8,stroke:#cc4444
    style PlanQ fill:#fff3e0,stroke:#e6a817
    style FailQ fill:#fce8e8,stroke:#cc4444
    style ChangedFiles fill:#d4edda,stroke:#28a745
```

Source of truth: `ai_tech_lead/coding_workflow_graph.py` (`NodeName` enum + `build_graph()`). This
replaces an earlier 9-step version of this diagram that had drifted from the real graph — it was
missing the entire plan-review loop (`request_plan` → `review_plan`, with up to 2 auto-retries
before asking you for guidance) and showed a generic "clarification" gate that doesn't actually
exist in the code.

## What each node does

| Node | Purpose | Can pause? |
|---|---|---|
| `1_read_request` | Validate the request isn't empty | No |
| `1b_check_research` | Does this task need local doc evidence? | No — decides routing only |
| `1c_research_interrupt` | Ask to fetch online docs when local evidence is insufficient | Yes — asks you |
| `1d_collect_research_evidence` | Fetch + cache the approved online sources | No |
| `2_review_risk` | Decide risk level and whether approval is required | No — flags but continues |
| `3c_tech_lead_analyse` | Formulate the bounded task + tech direction | No |
| `4_approval_interrupt` | Should a human approve before planning/execution? | Yes — `/approve` or `/reject` |
| `5b_request_plan` | Ask the coding agent for a high-level implementation plan | No |
| `5c_review_plan` | LLM checks the plan actually matches the task | No — routes to retry or human |
| `5d_plan_interrupt` | Ask you for guidance after 2 plan rejections, or if the reviewer is unavailable | Yes — asks you |
| `5_create_agent_instruction` | Build the bounded, safe prompt for the coding backend | No |
| `6_run_coding_agent` | Run Codex/Claude Code with the instruction | Yes — on repeated failure |
| `6b_failure_interrupt` | Ask you for guidance after 2 failed coding-agent retries | Yes — asks you |
| `7_end_node` | Log the final outcome and return the result | No |

## Known gap

When driven through the hub's subprocess path (rather than the AI Tech Lead bot directly),
`5d_plan_interrupt` and `6b_failure_interrupt` don't map to clean `needs_clarification` /
`approval_required` statuses in `agent_task_runner.py` — they currently fall through to a generic
`blocked` status, unlike the research and approval interrupts. Worth tightening if the hub ever
needs to resume a paused plan/failure interrupt the same way it resumes approvals today.
