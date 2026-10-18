from dotenv import load_dotenv
load_dotenv('/home/robvoto/projects/agent-hub/.env')
from agent_hub.orchestrator import get_task_run_store
store = get_task_run_store()
for run in store.list_runs(session_id="af058-atl-sequential-review-20261016"):
    print("run", run.id, run.state, run.updated_at, run.selected_agent_id)
    for event in store.list_events(run.id):
        print("transition", event.created_at, event.from_state, "->", event.to_state, event.detail)
    for event in store.list_progress_events(run.id):
        print("progress", event.created_at, event.event_type, event.phase, event.human_summary, event.validation_status)
