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
    project_id: str | None = None,
    project_contract_version: int | None = None,
    project_fingerprint: str | None = None,
    project_context: dict[str, Any] | None = None,
    references: list[str] | None = None,
    human_approved: bool = False,
    approval_token: str | None = None,
    resume: Any | None = None,
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON payload written to a subprocess specialist's input file.

    `references` are user-provided or Hub-observed pointers (file paths,
    URLs, ticket IDs, etc.) passed through uninterpreted — Hub does not
    parse or act on their contents, only relays them.

    `project_id`/`project_contract_version`/`project_fingerprint` are the
    canonical `ProjectContext` Hub resolved and validated for `project_root`
    (see `project_context.py`) — additive enrichment of the same project
    selection, only ever sent alongside a non-empty `project_root`. A
    specialist that doesn't recognize them ignores them like any other
    field it doesn't declare interest in.

    `project_context` is the versioned `{schema_version, project_root,
    references}` envelope (see `_resolve_project_context_schema_version` in
    orchestrator.py) — only ever built and passed in when the specialist's
    own manifest declared a `project_context_contract` and Hub found a
    schema_version they both support. It travels alongside the flat
    `project_root`/`references` fields, not instead of them, so specialists
    that haven't adopted it yet keep working unchanged.

    `resume` is an opaque value a specialist previously issued as its own
    `resume_token` when it paused for clarification. Hub relays it back
    unchanged on the resumed dispatch; it never inspects what's inside.

    `decision` answers a specialist's generic paused-decision block
    (`pending_decision`: a `prompt` plus named `options`). It carries
    `option` (one of the names the specialist itself last reported),
    optional `text`, and `actor`. Hub only ever relays the option name the
    user picked from that specialist-declared list — it never invents or
    interprets option names itself. The paused conversation is identified
    by resubmitting the same `request_id`, not a separate resume value.
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
        if project_id:
            envelope["project_id"] = project_id
        if project_contract_version is not None:
            envelope["project_contract_version"] = project_contract_version
        if project_fingerprint:
            envelope["project_fingerprint"] = project_fingerprint
    if references:
        envelope["references"] = list(references)
    if project_context is not None:
        envelope["project_context"] = project_context
    if human_approved:
        envelope["human_approved"] = True
        if approval_token:
            envelope["approval_token"] = approval_token
    if resume is not None:
        envelope["resume"] = resume
    if decision is not None:
        envelope["decision"] = decision
    return envelope
