"""Gateway dispatch tests for the Human MCP shell-approval commands.

These prove the three Telegram commands route to the shell-approval bridge and
never fall through to the orchestrator (a shell decision must not be treated as
a task message).
"""

from __future__ import annotations

from types import SimpleNamespace

import agent_hub.telegram_gateway as telegram_gateway
from agent_hub.telegram_gateway import TelegramGateway


def _gateway(monkeypatch, sent):
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        ),
    )
    orch = SimpleNamespace(
        pending_run=lambda: None,
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    return TelegramGateway("token-123", orch)


def test_shell_approvals_lists_pending(monkeypatch):
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    called: list[bool] = []
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "render_pending_shell_requests",
        lambda chat_id: called.append(chat_id) or "Pending shell approvals:\n- abc",
    )

    gateway._handle_message({"chat": {"id": 42}, "text": "/shell-approvals"})

    assert called == [42]
    assert sent == [
        {"chat_id": 42, "text": "Pending shell approvals:\n- abc", "parse_mode": None}
    ]


def test_shell_approve_dispatches_with_request_id(monkeypatch):
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    seen: list[tuple] = []
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "decide_shell_request",
        lambda chat_id, request_id, decision, reason="": seen.append(
            (chat_id, request_id, decision, reason)
        )
        or "Shell request abc approved.",
    )

    gateway._handle_message({"chat": {"id": 42}, "text": "/shell-approve abc"})

    assert seen == [(42, "abc", "approve", "")]
    assert sent[0]["text"] == "Shell request abc approved."
    assert sent[0]["parse_mode"] is None


def test_shell_approve_without_id_shows_usage(monkeypatch):
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    seen: list[tuple] = []
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "decide_shell_request",
        lambda *a, **k: seen.append(a),
    )

    gateway._handle_message({"chat": {"id": 42}, "text": "/shell-approve"})

    # No id means no decision is attempted.
    assert seen == []
    assert "Usage:" in sent[0]["text"]


def test_shell_deny_dispatches_reason(monkeypatch):
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    seen: list[tuple] = []
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "decide_shell_request",
        lambda chat_id, request_id, decision, reason="": seen.append(
            (chat_id, request_id, decision, reason)
        )
        or "Shell request abc denied.",
    )

    gateway._handle_message({"chat": {"id": 42}, "text": "/shell-deny abc not safe"})

    assert seen == [(42, "abc", "deny", "not safe")]
    assert sent[0]["text"] == "Shell request abc denied."


def test_shell_deny_without_reason(monkeypatch):
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    seen: list[tuple] = []
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "decide_shell_request",
        lambda chat_id, request_id, decision, reason="": seen.append(
            (chat_id, request_id, decision, reason)
        )
        or "Shell request abc denied.",
    )

    gateway._handle_message({"chat": {"id": 42}, "text": "/shell-deny abc"})

    assert seen == [(42, "abc", "deny", "")]


def test_shell_commands_never_reach_the_orchestrator(monkeypatch):
    """A shell decision is not a task message and must not dispatch work."""
    sent: list[dict] = []
    gateway = _gateway(monkeypatch, sent)
    invocations: list[str] = []

    def _invoke(text, **kwargs):
        invocations.append(text)
        return "dispatched"

    gateway._orchestrators[42] = SimpleNamespace(
        pending_run=lambda: None,
        invoke=_invoke,
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "render_pending_shell_requests",
        lambda chat_id: "No shell-approval requests are waiting.",
    )
    monkeypatch.setattr(
        telegram_gateway.shell_approvals,
        "decide_shell_request",
        lambda *a, **k: "Shell request abc approved.",
    )

    for command in ("/shell-approvals", "/shell-approve abc", "/shell-deny abc no"):
        gateway._handle_message({"chat": {"id": 42}, "text": command})

    assert invocations == []


def test_help_text_documents_shell_approval_commands() -> None:
    help_text = TelegramGateway._help_text()

    assert "/shell-approvals - list Human MCP shell commands" in help_text
    assert "/shell-approve <request-id>" in help_text
    assert "/shell-deny <request-id> [reason]" in help_text
