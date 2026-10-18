"""Owner-facing bridge between Human MCP shell approvals and the Agent Hub bot.

This module is deliberately *not* a signer. It never holds the approval channel
key and cannot mint a decision on its own. Its only powers are:

* list the shell-approval requests the MCP server has announced as awaiting an
  owner decision, and
* capture the owner's explicit approve/deny so that a **local, signed** decision
  envelope is written by the approval module's own key on this machine.

That split matters. If Hub could sign, an intruder who compromised Hub could
authorise host shell commands with no owner in the loop. Here Hub is only a
messenger and a capture point; the authority still rests with the signing key
that stays on the Windows box, and every decision names the exact operation it
approved (via ``operation_digest``) so it cannot be rebound to a different
command.

The trust root is the operator's Telegram chat, which the gateway already
guards with ``HUB_ALLOWED_CHAT_IDS``. This module adds a second, independent
gate: an optional allow-list of chat ids that may issue shell decisions, so a
chat permitted to talk to Hub is not automatically permitted to approve host
shell commands.

Configuration (all optional; absent means the bridge is disabled):

* ``HUMAN_MCP_APPROVAL_DIR``     – the approval store directory (must match the
  MCP server's). A missing/empty value keeps the bridge inert.
* ``HUMAN_MCP_APPROVAL_OWNER``   – the configured owner principal id. It is
  required, not defaulted: a decision must name the real owner, so an unset
  value disables the bridge rather than guessing an identity.
* ``HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS`` – comma-separated chat ids allowed
  to issue shell decisions. Empty means *nobody* may decide (fail closed); it
  never means "everyone".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_APPROVAL_DIR_ENV = "HUMAN_MCP_APPROVAL_DIR"
_OWNER_ENV = "HUMAN_MCP_APPROVAL_OWNER"
_ALLOWED_CHATS_ENV = "HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS"

_DECISIONS = ("approve", "deny")


@dataclass(frozen=True)
class PendingShellRequest:
    """One shell-approval request the owner can act on."""

    request_id: str
    summary: str
    expires_at: float
    operation_digest: str
    kind: str

    @property
    def expired(self) -> bool:
        return bool(self.expires_at) and self.expires_at < time.time()


def bridge_disabled_reason() -> str | None:
    """Return why the bridge is unavailable, or ``None`` when it is usable."""
    base_dir = os.getenv(_APPROVAL_DIR_ENV, "").strip()
    if not base_dir:
        return (
            f"{_APPROVAL_DIR_ENV} is not set; shell-approval decisions stay disabled."
        )
    if not Path(base_dir).is_dir():
        return f"{_APPROVAL_DIR_ENV} does not exist; shell-approval decisions stay disabled."
    if not os.getenv(_OWNER_ENV, "").strip():
        # Never guess the owner principal. A decision recorded under an invented
        # identity is not the owner's decision, so an unset owner keeps the whole
        # bridge inert instead of writing a misattributed envelope.
        return (
            f"{_OWNER_ENV} is not set; shell-approval decisions stay disabled so no "
            "decision is recorded under a guessed owner."
        )
    return None


def allowed_decision_chat_ids() -> set[int]:
    """Chat ids permitted to issue shell decisions.

    Empty by design: with no explicit allow-list nobody may decide, so a chat
    that is merely allowed to talk to Hub cannot approve host shell commands by
    default.
    """
    raw = os.getenv(_ALLOWED_CHATS_ENV, "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


def _load_channel() -> Any:
    """The signing channel the MCP server itself reads decisions from.

    This deliberately reuses ``ApprovalRuntime.channel`` rather than building a
    second channel from the base directory: the runtime nests its channel at
    ``<base>/channel``, so constructing one at ``<base>`` would write decisions
    into a directory the server never reads and they would be silently lost.
    Reusing the runtime's instance also guarantees one key, one clock and one
    directory across both sides.

    The import stays deferred so the gateway can start and report a clear
    disabled reason when the approval module is not installed, and so the key
    file is only touched when a decision is actually attempted.
    """
    return _load_runtime().channel


def _load_runtime() -> Any:
    import human_mcp_approvals as approvals

    base_dir = os.getenv(_APPROVAL_DIR_ENV, "").strip()
    owner_id = os.getenv(_OWNER_ENV, "").strip()
    return approvals.ApprovalRuntime(Path(base_dir), owner_principal_id=owner_id)


def list_pending_shell_requests() -> list[PendingShellRequest]:
    """Return the shell-approval requests still awaiting an owner decision."""
    reason = bridge_disabled_reason()
    if reason:
        return []
    try:
        runtime = _load_runtime()
        service = runtime.service
    except Exception:
        return []

    decided: set[str] = set()
    try:
        for view in service.store.list_requests():
            if getattr(view, "status", "") not in ("pending", "claimed"):
                decided.add(view.request_id)
    except Exception:
        # A store that cannot be read simply reports nothing pending; it never
        # invents an approvable list.
        return []

    pending: list[PendingShellRequest] = []
    try:
        announcements = runtime.channel.read_announcements()
    except Exception:
        return []
    for entry in announcements:
        if not isinstance(entry, dict):
            continue
        request_id = str(entry.get("request_id") or "")
        if not request_id or request_id in decided:
            continue
        try:
            expires_at = float(entry.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        pending.append(
            PendingShellRequest(
                request_id=request_id,
                summary=str(entry.get("summary") or "(no summary)"),
                expires_at=expires_at,
                operation_digest=str(entry.get("operation_digest") or ""),
                kind=str(entry.get("kind") or "exact"),
            )
        )
    # Oldest first: the owner works through the queue in the order requests were
    # raised, and an expired request is shown as such rather than silently dropped.
    pending.sort(key=lambda item: item.expires_at)
    return pending


def render_pending_shell_requests(chat_id: int) -> str:
    """Render the pending queue for the operator.

    Listing is gated by the same allow-list as deciding: an operator chat that
    cannot approve a host shell command should not be able to read the pending
    command summaries either (``HUB_ALLOWED_CHAT_IDS`` may be empty, which lets
    any chat talk to the bot).
    """
    reason = bridge_disabled_reason()
    if reason:
        return f"Shell approvals unavailable: {reason}"
    if chat_id not in allowed_decision_chat_ids():
        return "This chat is not permitted to view shell approvals."
    pending = list_pending_shell_requests()
    if not pending:
        return "No shell-approval requests are waiting."
    lines = ["Pending shell approvals:"]
    for item in pending:
        state = "EXPIRED" if item.expired else "pending"
        lines.append(
            f"- {item.request_id} [{state}] {item.summary}"
        )
    lines.append("")
    lines.append("Approve with: /shell-approve <request-id>")
    lines.append("Deny with:     /shell-deny <request-id> [reason]")
    return "\n".join(lines)


def decide_shell_request(chat_id: int, request_id: str, decision: str, reason: str = "") -> str:
    """Capture the owner's explicit decision and write a local signed envelope.

    Returns an operator-facing message. Any failure is reported rather than
    swallowed, and a failure never produces an approving decision.
    """
    if decision not in _DECISIONS:
        return f"Invalid shell decision {decision!r}."

    disabled = bridge_disabled_reason()
    if disabled:
        return f"Shell approvals unavailable: {disabled}"

    if chat_id not in allowed_decision_chat_ids():
        # Independent of the gateway's chat gate: being allowed to talk to Hub
        # does not imply authority over host shell commands.
        return "This chat is not permitted to decide shell approvals."

    request_id = (request_id or "").strip()
    if not request_id:
        return "A request id is required. Use /shell-approvals to list pending requests."

    try:
        pending = {item.request_id: item for item in list_pending_shell_requests()}
    except Exception:
        pending = {}
    item = pending.get(request_id)
    if item is None:
        return (
            f"No pending shell request {request_id!r}. "
            "Use /shell-approvals to list the current queue."
        )
    if item.expired:
        return f"Shell request {request_id} has expired and can no longer be decided."

    owner_id = os.getenv(_OWNER_ENV, "").strip()
    try:
        channel = _load_channel()
        channel.write_decision(
            request_id=request_id,
            kind=item.kind if item.kind in ("exact", "task") else "exact",
            decision=decision,
            owner_id=owner_id,
            reason=reason or None,
            operation_digest=item.operation_digest,
        )
    except Exception as exc:
        # Never echo internals (paths, key names) back to the chat.
        return f"Could not record the shell decision: {type(exc).__name__}. Nothing was approved."

    verb = "approved" if decision == "approve" else "denied"
    return (
        f"Shell request {request_id} {verb}. The MCP server records the decision; "
        f"a single-use handle is issued only if you approved it."
    )
