from dotenv import load_dotenv

load_dotenv('/home/robvoto/projects/agent-hub/.env')

from agent_hub.cli import main

main(['chat'])
