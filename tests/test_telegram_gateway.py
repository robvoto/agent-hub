"""Tests for Telegram command handling."""

from __future__ import annotations

from types import SimpleNamespace

from agent_hub.telegram_gateway import TelegramGateway


def test_status_command_sends_plain_text_status(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "Current task:\nState: in_progress",
        last_run_status=lambda: "unused",
        learn=lambda value, *, source: f"Stored learning from {source}: {value}",
        memory=lambda: "Stored hub learnings:\nmem-1\n  example",
        forget_learning=lambda identifier: f"Forgot learning {identifier}.",
        stop_current_task=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/status"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Current task:\nState: in_progress",
            "parse_mode": None,
        }
    ]


def test_last_command_sends_plain_text_summary(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "unused",
        last_run_status=lambda: "Last task:\nState: failed",
        learn=lambda value, *, source: f"Stored learning from {source}: {value}",
        memory=lambda: "Stored hub learnings:\nmem-1\n  example",
        forget_learning=lambda identifier: f"Forgot learning {identifier}.",
        stop_current_task=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/last"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Last task:\nState: failed",
            "parse_mode": None,
        }
    ]


def test_stop_command_sends_plain_text_confirmation(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "unused",
        last_run_status=lambda: "unused",
        learn=lambda value, *, source: f"Stored learning from {source}: {value}",
        memory=lambda: "Stored hub learnings:\nmem-1\n  example",
        forget_learning=lambda identifier: f"Forgot learning {identifier}.",
        stop_current_task=(
            lambda: "Stopped run run-123 for agent 'ai-tech-lead'. State is now cancelled."
        ),
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/stop"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Stopped run run-123 for agent 'ai-tech-lead'. State is now cancelled.",
            "parse_mode": None,
        }
    ]


def test_learn_command_sends_plain_text_confirmation(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "unused",
        last_run_status=lambda: "unused",
        learn=lambda value, *, source: f"Stored learning from {source}: {value}",
        memory=lambda: "unused",
        forget_learning=lambda identifier: f"unused {identifier}",
        stop_current_task=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/learn keep approvals explicit"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Stored learning from telegram chat 42: keep approvals explicit",
            "parse_mode": None,
        }
    ]


def test_memory_command_sends_plain_text_listing(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "unused",
        last_run_status=lambda: "unused",
        learn=lambda value, *, source: f"unused {value} {source}",
        memory=lambda: "Stored hub learnings:\nmem-1\n  example",
        forget_learning=lambda identifier: f"unused {identifier}",
        stop_current_task=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/memory"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Stored hub learnings:\nmem-1\n  example",
            "parse_mode": None,
        }
    ]


def test_forget_command_sends_plain_text_confirmation(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {
                "token": token,
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        ),
    )

    orch = SimpleNamespace(
        current_run_status=lambda: "unused",
        last_run_status=lambda: "unused",
        learn=lambda value, *, source: f"unused {value} {source}",
        memory=lambda: "unused",
        forget_learning=lambda identifier: f"Forgot learning {identifier}.",
        stop_current_task=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/forget mem-123"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Forgot learning mem-123.",
            "parse_mode": None,
        }
    ]


def test_learn_mode_command_toggles_and_reports_status(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"text": text, "parse_mode": parse_mode}
        ),
    )

    calls: list[bool] = []
    orch = SimpleNamespace(
        registry=[],
        set_learning_notifier=lambda callback: None,
        set_learning_mode=lambda enabled: calls.append(enabled)
        or f"Learning mode is now {'ON' if enabled else 'OFF'}.",
        learning_mode_status=lambda: "Learning mode is OFF.",
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/learn-mode"})
    gateway._handle_message({"chat": {"id": 42}, "text": "/learn-mode on"})
    gateway._handle_message({"chat": {"id": 42}, "text": "/learn-mode off"})
    gateway._handle_message({"chat": {"id": 42}, "text": "/learn-mode bogus"})

    assert calls == [True, False]
    assert [s["text"] for s in sent] == [
        "Learning mode is OFF.",
        "Learning mode is now ON.",
        "Learning mode is now OFF.",
        "Usage: /learn-mode [on|off]",
    ]


def test_project_command_sets_shows_and_clears(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"text": text, "parse_mode": parse_mode}
        ),
    )

    calls: list[str] = []
    orch = SimpleNamespace(
        registry=[],
        set_learning_notifier=lambda callback: None,
        current_project_status=lambda: "No project selected.",
        set_current_project=lambda path: calls.append(("set", path))
        or f"Current project set to {path}.",
        clear_current_project=lambda: calls.append(("clear",))
        or "Current project cleared.",
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/project"})
    gateway._handle_message({"chat": {"id": 42}, "text": "/project /some/repo"})
    gateway._handle_message({"chat": {"id": 42}, "text": "/project clear"})

    assert calls == [("set", "/some/repo"), ("clear",)]
    assert [s["text"] for s in sent] == [
        "No project selected.",
        "Current project set to /some/repo.",
        "Current project cleared.",
    ]


def test_learning_notifier_sends_to_last_seen_chat(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text}
        ),
    )

    captured_notifier: list = []
    orch = SimpleNamespace(
        registry=[],
        set_learning_notifier=lambda callback: captured_notifier.append(callback),
        current_run_status=lambda: "unused",
    )
    gateway = TelegramGateway("token-123", orch)
    gateway._handle_message({"chat": {"id": 42}, "text": "/status"})

    assert captured_notifier
    captured_notifier[0]("\U0001f9e0 Learned: prefers tabs")

    assert sent[-1] == {"chat_id": 42, "text": "\U0001f9e0 Learned: prefers tabs"}
