"""The universal task envelope Hub sends to every subprocess specialist.

This is the one contract every specialist is dispatched through, regardless
of who it is or what it does internally. Hub populates only what it already
knows (task identity, run/request identity, source, execution mode, selected
project, resume/approval state, and any references the user or Hub observed)
and never interprets specialist-specific business fields. A specialist that
doesn't recognize a field simply ignores it — Hub does not vary what it sends
based on which specialist is being called.
"""

from __future__ import annotations

from typing import Any


def build_task_envelope(
    *,
    task: str,
    request_id: str,
    run_id: str | None,
    source: str,
    execution_mode: str,
    progress_jsonl: str,
    project_root: str | None = None,
    references: list[str] | None = None,
    human_approved: bool = False,
    approval_token: str | None = None,
    resume: Any | None = None,
) -> dict[str, Any]:
    """Build the JSON payload written to a subprocess specialist's input file.

    `references` are user-provided or Hub-observed pointers (file paths,
    URLs, ticket IDs, etc.) passed through uninterpreted — Hub does not
    parse or act on their contents, only relays them.

    `resume` is an opaque value a specialist previously issued as its own
    `resume_token` when it paused for clarification. Hub relays it back
    unchanged on the resumed dispatch; it never inspects what's inside.
    """
    envelope: dict[str, Any] = {
        "request_id": request_id,
        "run_id": run_id,
        "task": task,
        "source": source,
        "execution_mode": execution_mode,
        "progress_jsonl": progress_jsonl,
    }
    if project_root:
        envelope["project_root"] = project_root
    if references:
        envelope["references"] = list(references)
    if human_approved:
        envelope["human_approved"] = True
        if approval_token:
            envelope["approval_token"] = approval_token
    if resume is not None:
        envelope["resume"] = resume
    return envelope
