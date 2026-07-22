"""Contract-only stub specialist CLI.

Not a fake of any real agent's reasoning. It only implements the
subprocess runtime shape hub's registry.runtime.mode == "subprocess"
agents are required to speak: read --input-json, write --output-json,
using the same output field names ai-tech-lead's manifest declares
(status/result_kind/caller_action/resume_supported/resume_fields/
interrupt_kind). Used to exercise agent_hub.orchestrator._dispatch_subprocess
against a real child process instead of a mocked subprocess.Popen.

Scenario selection: the caller's "task" string may start with
"SCENARIO:<name>" to pick a canned response. Default is plain success.
"""

from __future__ import annotations

import argparse
import json
import sys

_BASE_FIELDS = {
    "formulated_task": "",
    "brief": "",
    "coding_agent_instruction": "",
    "backend_used": "none",
    "execution_performed": False,
    "logs": [],
    "evidence": [],
    "next_action": "",
    "agent_manifest": None,
}


def _scenario_response(scenario: str, input_data: dict) -> dict:
    if input_data.get("human_approved"):
        return {
            **_BASE_FIELDS,
            "status": "success",
            "summary": "Approved task completed.",
            "result_kind": "execution_result",
            "caller_action": "consume_result",
            "resume_supported": False,
            "resume_fields": [],
            "interrupt_kind": "",
        }

    if scenario == "needs_clarification":
        return {
            **_BASE_FIELDS,
            "status": "needs_clarification",
            "summary": "Clarification needed: which environment?",
            "result_kind": "clarification_request",
            "caller_action": "provide_clarification",
            "resume_supported": True,
            "resume_fields": ["request_id", "task"],
            "interrupt_kind": "orchestrator_question",
        }

    if scenario == "approval_required":
        return {
            **_BASE_FIELDS,
            "status": "approval_required",
            "summary": "Approval required: deletes files.",
            "approval_token": "stub-approval-token",
            "result_kind": "approval_request",
            "caller_action": "provide_approval",
            "resume_supported": True,
            "resume_fields": ["request_id", "task", "human_approved", "approval_token"],
            "interrupt_kind": "approval_required",
        }

    if scenario == "failed":
        return {
            **_BASE_FIELDS,
            "status": "failed",
            "summary": "Stub terminal failure.",
            "result_kind": "terminal_failure",
            "caller_action": "inspect_failure",
            "resume_supported": False,
            "resume_fields": [],
            "interrupt_kind": "",
        }

    return {
        **_BASE_FIELDS,
        "status": "success",
        "summary": "Stub task completed.",
        "coding_agent_instruction": "echo done",
        "result_kind": "execution_result",
        "caller_action": "consume_result",
        "resume_supported": False,
        "resume_fields": [],
        "interrupt_kind": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-json", required=True)
    args = parser.parse_args()

    input_data = json.loads(open(args.input_json, encoding="utf-8").read())
    task = str(input_data.get("task", ""))
    scenario = ""
    if task.startswith("SCENARIO:"):
        scenario = task.split(":", 1)[1].split(None, 1)[0]

    response = {
        "request_id": input_data.get("request_id", ""),
        "received_project_root": input_data.get("project_root"),
    }
    response.update(_scenario_response(scenario, input_data))

    with open(args.output_json, "w", encoding="utf-8") as fh:
        json.dump(response, fh)


if __name__ == "__main__":
    main()
