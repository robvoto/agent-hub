"""Tests for Telegram command handling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_hub.progress_events import ProgressUpdate
from agent_hub.task_control import get_task_control_registry
from agent_hub.task_runs import TASK_STATE_CANCELLED, get_task_run_store
from agent_hub.telegram_gateway import TelegramGateway, _raise_keyboard_interrupt


def test_help_text_explains_current_thread_controls() -> None:
    help_text = TelegramGateway._help_text()

    assert "Reply normally to continue a clarification pause in the same thread." in help_text
    assert "Use /approve to continue an approval pause in the same thread." in help_text
    assert "/decide <option> [text] - answer a task waiting on a specialist decision" in help_text
    assert "Use /decide <option> [text] to continue a decision pause" in help_text
    assert "/new starts a fresh empty thread; it is not a fork." in help_text
    assert "Cancelled work from /stop or /reset is not resumable." in help_text
    assert "There is no /fork or generic /resume command yet." in help_text


def test_decide_command_forwards_option_and_text_to_orchestrator(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text}
        ),
    )

    calls: list[tuple[str, str]] = []

    def _provide_decision(option, text, *, progress_notify=None):
        calls.append((option, text))
        return f"[Widget Forge] decided: {option} ({text!r})"

    orch = SimpleNamespace(
        provide_decision=_provide_decision,
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message(
        {"chat": {"id": 42}, "text": "/decide request_changes Make them square"}
    )

    assert calls == [("request_changes", "Make them square")]
    assert sent == [
        {"chat_id": 42, "text": "[Widget Forge] decided: request_changes ('Make them square')"}
    ]


def test_decide_command_without_an_option_shows_usage(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text}
        ),
    )

    orch = SimpleNamespace(set_learning_notifier=lambda callback: None)
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/decide"})

    assert sent == [{"chat_id": 42, "text": "Usage: /decide <option> [text]"}]


def test_new_command_resets_session_once_and_sends_one_reply(monkeypatch):
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

    calls: list[str] = []
    orch = SimpleNamespace(
        new_session=lambda: calls.append("new_session"),
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/new"})

    assert calls == ["new_session"]
    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Started a fresh conversation.",
            "parse_mode": "Markdown",
        }
    ]


def test_reset_command_stops_active_work_and_sends_one_reply(monkeypatch):
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

    calls: list[str] = []
    orch = SimpleNamespace(
        reset_session=lambda: calls.append("reset_session")
        or "Reset complete. Stopped the active task and started a fresh conversation.",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/reset"})

    assert calls == ["reset_session"]
    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Reset complete. Stopped the active task and started a fresh conversation.",
            "parse_mode": None,
        }
    ]


def test_telegram_message_uses_human_logger(caplog, monkeypatch):
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": None,
    )

    orch = SimpleNamespace(
        new_session=lambda: None,
        reset_session=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        gateway._handle_message({"chat": {"id": 42}, "text": "/new"})

    assert "Telegram message from chat 42: /new" in caplog.text


def test_prime_offset_skips_only_stale_queued_updates_and_advances_offset(monkeypatch):
    old_timestamp = time.time() - 3600  # well past the startup staleness window

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._get_updates",
        lambda token, offset: [
            {
                "update_id": 100,
                "message": {"chat": {"id": 42}, "text": "/new", "date": old_timestamp},
            },
            {
                "update_id": 104,
                "message": {"chat": {"id": 42}, "text": "/status", "date": old_timestamp},
            },
        ]
        if offset == 0
        else [],
    )

    orch = SimpleNamespace(
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    assert gateway._prime_offset() == 105


def test_prime_offset_processes_fresh_queued_update_instead_of_dropping_it(monkeypatch):
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": None,
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._get_updates",
        lambda token, offset: [
            {
                "update_id": 100,
                "message": {"chat": {"id": 42}, "text": "/new", "date": time.time()},
            },
        ]
        if offset == 0
        else [],
    )

    calls: list[str] = []
    orch = SimpleNamespace(
        new_session=lambda: calls.append("new_session"),
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    assert gateway._prime_offset() == 101
    assert calls == ["new_session"]


def test_duplicate_telegram_message_is_ignored(monkeypatch):
    sent: list[str] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(text),
    )

    calls: list[str] = []
    orch = SimpleNamespace(
        new_session=lambda: calls.append("new_session"),
        reset_session=lambda: "unused",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_update(
        {"update_id": 100, "message": {"message_id": 55, "chat": {"id": 42}, "text": "/new"}}
    )
    gateway._handle_update(
        {"update_id": 101, "message": {"message_id": 55, "chat": {"id": 42}, "text": "/new"}}
    )

    assert calls == ["new_session"]
    assert sent == ["Started a fresh conversation."]


def test_run_logs_api_base_url_in_human_log(monkeypatch, caplog):
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._API_BASE",
        "https://example-telegram.invalid",
    )
    monkeypatch.setattr("agent_hub.telegram_gateway.os.getpid", lambda: 424242)
    monkeypatch.setattr(
        TelegramGateway,
        "_prime_offset",
        lambda self: 0,
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._get_updates",
        lambda token, offset: [],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway.time.sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    orch = SimpleNamespace(
        reset_session=lambda: "unused",
        registry=[],
        session_id="session-123",
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        gateway.run()

    assert "pid=424242" in caplog.text
    assert "api=https://example-telegram.invalid" in caplog.text


def test_run_cancels_in_flight_tasks_on_keyboard_interrupt(monkeypatch, caplog):
    monkeypatch.setattr(TelegramGateway, "_prime_offset", lambda self: 0)
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._get_updates",
        lambda token, offset: [],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway.time.sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    run = get_task_run_store().create_run(session_id="s1", user_message="hi")
    get_task_control_registry().register_run(run.id)

    orch = SimpleNamespace(
        reset_session=lambda: "unused",
        registry=[],
        session_id="session-123",
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    with caplog.at_level(logging.INFO, logger="agent_hub.human"):
        gateway.run()

    assert f"Cancelled 1 in-flight task(s) on shutdown: {run.id[:8]}" in caplog.text
    updated = get_task_run_store().get_run(run.id)
    assert updated is not None
    assert updated.state == TASK_STATE_CANCELLED


def test_run_installs_and_restores_sigterm_handler(monkeypatch):
    import signal

    monkeypatch.setattr(TelegramGateway, "_prime_offset", lambda self: 0)
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._get_updates",
        lambda token, offset: [],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway.time.sleep",
        lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    original_handler = signal.getsignal(signal.SIGTERM)
    orch = SimpleNamespace(
        reset_session=lambda: "unused",
        registry=[],
        session_id="session-123",
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway.run()

    assert signal.getsignal(signal.SIGTERM) is original_handler


def test_raise_keyboard_interrupt_converts_signal_to_exception():
    with pytest.raises(KeyboardInterrupt):
        _raise_keyboard_interrupt(15, None)


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
        reset_session=lambda: "unused",
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


def test_agents_status_command_sends_plain_text_report(monkeypatch):
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
        agents_status=lambda: "Registry last refreshed at ... — 1 agent(s) active.",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/agents-status"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Registry last refreshed at ... — 1 agent(s) active.",
            "parse_mode": None,
        }
    ]


def test_agents_refresh_command_sends_plain_text_report(monkeypatch):
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
        refresh_registry=lambda: "Registry refreshed — 1 agent(s) active.\nAdded: none",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/agents-refresh"})

    assert sent == [
        {
            "token": "token-123",
            "chat_id": 42,
            "text": "Registry refreshed — 1 agent(s) active.\nAdded: none",
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
        reset_session=lambda: "unused",
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
        reset_session=lambda: "unused",
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
        reset_session=lambda: "unused",
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


def test_progress_notifier_deduplicates_repeated_non_heartbeat_updates(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        ),
    )

    orch = SimpleNamespace(
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    now = datetime.now(timezone.utc)

    repeated = ProgressUpdate(
        run_id="run-1",
        event_type="phase",
        phase="editing",
        human_summary="Applying the requested change.",
        occurred_at=now,
        sequence=1,
    )
    heartbeat = ProgressUpdate(
        run_id="run-1",
        event_type="heartbeat",
        phase="editing",
        human_summary="Still working: editing.",
        occurred_at=now,
    )

    gateway._notify_progress(42, repeated)
    gateway._notify_progress(42, repeated)
    gateway._notify_progress(42, heartbeat)
    gateway._notify_progress(42, heartbeat)

    assert sent == [
        {"chat_id": 42, "text": "Applying the requested change.", "parse_mode": None},
        {"chat_id": 42, "text": "Still working: editing.", "parse_mode": None},
        {"chat_id": 42, "text": "Still working: editing.", "parse_mode": None},
    ]
