import json, tempfile, traceback
from pathlib import Path
from agent_hub.factory_bridge import build_factory_agent_spec, resolve_factory_build_task
from agent_hub.handoff_transition import ApprovedDesignEvidence, FactoryHandoffEvidenceResolver, NextTaskContract, factory_execution_constraints
from agent_hub.orchestrator import _dispatch_subprocess
from agent_hub.registry import load_registry_report
from agent_hub.task_runs import (
    get_task_run_store, active_task_run, TASK_STATE_FAILED, TASK_STATE_SUCCEEDED,
    TASK_STATE_WAITING_APPROVAL, TASK_STATE_WAITING_CLARIFICATION, TASK_STATE_WAITING_DECISION,
)
factory = Path("/home/robvoto/projects/agent-factory-wt-handoff-controls")
thread = "hub-factory-7f764ee1-c920-4654-a45d-e772591b7777"
reference = "staging/agents/shopping-agent/BUILD_TASK.json"
resolved = resolve_factory_build_task(working_directory=str(factory), thread_id=thread, references=[reference])
next_task = NextTaskContract.model_validate(resolved["factory_result"]["next_task"])
factory_spec = build_factory_agent_spec(factory)
resolver = FactoryHandoffEvidenceResolver(factory)
project = resolver.target_project_context(next_task.references, None, originating_agent_id=factory_spec.id)
evidence = ApprovedDesignEvidence.model_validate(resolver.resolve(
    next_task.references, next_task, project, originating_agent_id=factory_spec.id, factory_thread_id=thread))
constraints = factory_execution_constraints(evidence)
task = next_task.task + "\\n\\nRob approved this exact Factory build task in ChatGPT. The approval includes the product behavior and classification rules written in the approved BUILD_TASK; do not ask for a second approval of those same requirements. The target is the authorized isolated Factory worktree. Creating staging/agents/shopping-agent/src/ and real tests under staging/agents/shopping-agent/tests/ is explicitly permitted and required; the directories are currently absent/empty. Do not finish with an empty patch. The exact pytest command must collect and pass real offline tests. Do not invent unresolved operational numeric defaults: keep them configurable/unset and explain in README because live calls and dry-run measurement are prohibited for this task. Preserve null whole-task token/time budgets and all other task boundaries."
registry = load_registry_report(factory / "config" / "agents")
if registry.errors:
    raise RuntimeError("Invalid enabled registry: " + str(registry.errors))
spec = next(s for s in registry.specs if s.id == "ai-tech-lead")
with tempfile.TemporaryDirectory(prefix="af058-hub-run-") as td:
    store = get_task_run_store(Path(td) / "task-runs.sqlite3")
    run = store.create_run(session_id="af058-" + constraints["correlation_id"],
                           user_message="Execute approved AF-058 Factory build task " + constraints["correlation_id"])
    def progress(update):
        value = update.model_dump(mode="json") if hasattr(update, "model_dump") else str(update)
        print("HUB_PROGRESS " + json.dumps(value, ensure_ascii=False), flush=True)
    try:
        with active_task_run(run.id, progress_callback=progress):
            result = _dispatch_subprocess(
                spec, task, references=list(next_task.references), human_approved=True,
                execution_constraints=constraints, project_root_override=project.root,
                project_context_override=project, task_kind=next_task.task_kind,
                request_id="5085afc2-182f-479a-9ea0-53dde506398e",
                decision={"option": "reject", "text": "Not accepted: independent review found five required safety defects: max_retries is not enforced; a missing later provider cost can leave an incomplete subtotal that still permits calls under a configured cost cap; retailer Product evidence is not matched to the discovered/requested item; raw task text is sent as a SerpApi query without secret-like redaction; and warning thresholds are emitted only after workflow completion. Keep this execution unaccepted."},
                result_registry=registry.specs)
    except Exception:
        print("DISPATCH_EXCEPTION " + traceback.format_exc(), flush=True)
        raise
    print("ATL_RAW_RESULT " + json.dumps(result, ensure_ascii=False, default=str), flush=True)
    current = store.get_run(run.id)
    print("HUB_RUN_STATE " + str(current.state), flush=True)
    # The Hub task-run context already applies the ATL terminal/pause transition.
    # Do not attempt a duplicate transition here.
    _ = (TASK_STATE_FAILED, TASK_STATE_SUCCEEDED, TASK_STATE_WAITING_APPROVAL,
         TASK_STATE_WAITING_CLARIFICATION, TASK_STATE_WAITING_DECISION)
    print("AF058_HUB_RECEIPT " + json.dumps({
        "hub_run_id": run.id, "status": result.get("status"),
        "correlation_id": constraints["correlation_id"]
    }, ensure_ascii=False), flush=True)
