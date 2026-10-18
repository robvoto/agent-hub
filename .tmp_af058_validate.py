from dotenv import load_dotenv
load_dotenv("/home/robvoto/projects/agent-hub/.env")

from agent_hub.orchestrator import HubOrchestrator
from agent_hub.config import AGENT_FACTORY_ROOT, configured_model

hub = HubOrchestrator(model=configured_model())
status = hub.tasks_status()
matches = [line for line in str(status).splitlines() if any(
    needle in line.casefold()
    for needle in ("385316d767389a5ecea7a33ab54cf56b", "shopping-agent", "af-058", "af058")
)]
print("factory_root=" + str(AGENT_FACTORY_ROOT))
print("matching_task_lines=" + repr(matches))
