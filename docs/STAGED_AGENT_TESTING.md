# Testing staged agents through Agent Hub

A staged agent can remain in testing for as long as needed. Staging is a working state, not a timer or an automatic promotion queue.

## Hub profiles

Agent Hub defaults to the existing `live` profile. That profile continues to load only Factory's enabled registry at `config/agents`.

To run an isolated staged-agent test profile, configure the Hub process:

```sh
export HUB_PROFILE=test
export HUB_STAGED_AGENT_IDS=shopping-agent
export AGENT_FACTORY_ROOT=/path/to/agent-factory-test-checkout
export HUB_DATA_DIR="$HOME/.local/share/agent-hub/test"
```

`HUB_STAGED_AGENT_IDS` is an explicit comma-separated allowlist. Test mode does not load enabled agents or Factory itself. Hub run databases and other Hub-owned data use the directory selected by `HUB_DATA_DIR`; if omitted, test mode stores its data in a separate per-user test folder. Test mode rejects a data folder that overlaps the live Hub data folder.

The Hub can be started in the usual interface, including Telegram:

```sh
uv run agent-hub telegram
```

For the Shopping draft, also set `SHOPPING_MAX_API_CALLS`, `SHOPPING_MAX_PAGE_FETCHES`, and `SHOPPING_TIMEOUT_SECONDS` in that Hub process. These must be explicit values chosen for the test; the adapter does not guess them. The staged process receives only its declared provider credential and these limits, not Hub's Telegram or model credentials.

With a Telegram gateway, use `/hub-status`, `/agents`, and `/agents-status` to confirm the active profile, the staged source, and any reason an agent is not callable. Every callable candidate is labelled `[STAGED TEST]`. The Telegram bot token must have only one active polling process at a time.

Hub can’t run a draft until it has a Hub connector. Once connected, select it in the test profile and run your test through Agent Hub.

## Readiness evidence

Use the Hub task history as evidence against the candidate's declared acceptance criteria. Record or review:

- which exact agent version and manifest fingerprint was tested;
- representative normal, boundary, and failure cases from the agent's acceptance criteria;
- successful outcomes, failures, retries, and cases needing human correction;
- permission and side-effect behavior, including whether each action was expected;
- remaining defects and the evidence that each was fixed.

A useful promotion decision is made when the agreed cases have been exercised, important cases are repeatable, there are no unresolved critical failures, and a human has reviewed the recorded Hub runs. The required number of runs should match the agent's risk and acceptance criteria rather than a universal timer.

Promotion remains a separate explicit Factory lifecycle action. A test profile or a good test run does not automatically make an agent live.
