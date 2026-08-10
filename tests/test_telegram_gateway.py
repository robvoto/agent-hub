"""Tests for Telegram command handling."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import agent_hub.singleton_lock as singleton_lock
from agent_hub.progress_events import ProgressUpdate
from agent_hub.singleton_lock import SingletonLockBusyError
from agent_hub.task_control import get_task_control_registry
from agent_hub.task_runs import TASK_STATE_CANCELLED, get_task_run_store
from agent_hub.telegram_gateway import TelegramGateway, _raise_keyboard_interrupt, run_telegram


def test_help_text_explains_current_thread_controls() -> None:
    help_text = TelegramGateway._help_text()

    assert "Reply normally to continue a clarification pause in the same thread." in help_text
    assert "Use /approve to continue an approval pause in the same thread." in help_text
    assert "/decide" not in help_text
    assert "Reply with the option number or name to continue a decision pause" in help_text
    assert "/new starts a fresh empty thread; it is not a fork." in help_text
    assert "Cancelled work from /stop or /reset is not resumable." in help_text
    assert "There is no /fork or generic /resume command yet." in help_text
    assert "/hub-status - show the hub startup summary" in help_text


def test_hub_status_command_reports_summary_without_starting_new_session(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        ),
    )

    calls: list[str] = []
    orch = SimpleNamespace(
        hub_status=lambda: calls.append("hub_status")
        or "Agent Hub status\n\nLearning: ON\nProject: agent-hub\n"
        "Agents available: 2\nActive task: none\nMemory records: 14",
        registry=[],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._handle_message({"chat": {"id": 42}, "text": "/hub-status"})

    assert calls == ["hub_status"]
    assert sent == [
        {
            "chat_id": 42,
            "text": "Agent Hub status\n\nLearning: ON\nProject: agent-hub\n"
            "Agents available: 2\nActive task: none\nMemory records: 14",
            "parse_mode": None,
        }
    ]


def test_plain_decision_reply_is_forwarded_to_orchestrator(monkeypatch):
    sent: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text}
        ),
    )

    calls: list[str] = []

    def _provide_decision_reply(reply, *, progress_notify=None):
        calls.append(reply)
        return "[Widget Forge] decision accepted"

    orch = SimpleNamespace(
        pending_run=lambda: SimpleNamespace(state="waiting_decision"),
        provide_decision_reply=_provide_decision_reply,
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    gateway._process_user_message(42, "1 Make them square")

    assert calls == ["1 Make them square"]
    assert sent == [{"chat_id": 42, "text": "[Widget Forge] decision accepted"}]


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
        new_session=lambda: calls.append("new_session")
        or "New Agent Hub session\n\nLearning: OFF\nProject: none\n"
        "Agents available: 0\nActive task: none\nMemory records: 0",
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
            "text": "New Agent Hub session\n\nLearning: OFF\nProject: none\n"
            "Agents available: 0\nActive task: none\nMemory records: 0",
            "parse_mode": None,
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
        new_session=lambda: calls.append("new_session") or "New Agent Hub session",
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
    assert sent == ["New Agent Hub session"]


def test_run_logs_api_base_url_in_human_log(monkeypatch, caplog):
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._API_BASE",
        "https://example-telegram.invalid",
    )
    monkeypatch.setattr("agent_hub.telegram_gateway.os.getpid", lambda: 424242)
    monkeypatch.setattr(
        TelegramGateway,
        "_prime_offset",
        lambda self, registry=None, **kwargs: 0,
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
    monkeypatch.setattr(TelegramGateway, "_prime_offset", lambda self, registry=None, **kwargs: 0)
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

    monkeypatch.setattr(TelegramGateway, "_prime_offset", lambda self, registry=None, **kwargs: 0)
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


def _create_active_run(*, agent_id: str = "ai-tech-lead") -> str:
    store = get_task_run_store()
    run = store.create_run(session_id="telegram:42", user_message="Do the work")
    store.transition(run.id, "routed", selected_agent_id=agent_id)
    store.transition(run.id, "dispatched", selected_agent_id=agent_id)
    return run.id


def test_progress_notifier_keeps_one_live_message_and_edits_meaningful_updates(monkeypatch):
    sent: list[dict] = []
    edited: list[dict] = []
    next_message_id = 100

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [next_message_id],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        or True,
    )

    orch = SimpleNamespace(
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    now = datetime.now(timezone.utc)
    run_id = _create_active_run()

    start = ProgressUpdate(
        run_id=run_id,
        event_type="start",
        phase="starting",
        human_summary="AI Tech Lead started.",
        occurred_at=now,
    )
    first = ProgressUpdate(
        run_id=run_id,
        event_type="phase",
        phase="editing",
        human_summary="Applying the requested change.",
        occurred_at=now,
        sequence=1,
    )
    duplicate = ProgressUpdate(
        run_id=run_id,
        event_type="phase",
        phase="reviewing",
        human_summary="Applying the requested change.",
        occurred_at=now + timedelta(seconds=20),
        sequence=2,
    )

    gateway._notify_progress(42, start)
    gateway._notify_progress(42, first)
    gateway._notify_progress(42, duplicate)

    assert sent == [
        {
            "chat_id": 42,
            "text": "AI Tech Lead is working\n\nElapsed: under 1 minute",
            "parse_mode": None,
        }
    ]
    assert edited == [
        {
            "chat_id": 42,
            "message_id": 100,
            "text": "AI Tech Lead is working\n\nApplying the requested change.\n\nElapsed: under 1 minute",
            "parse_mode": None,
        }
    ]
    run = get_task_run_store().get_run(run_id)
    assert run is not None
    assert run.context["telegram_live_chat_id"] == 42
    assert run.context["telegram_live_message_id"] == 100


def test_heartbeat_refresh_edits_elapsed_without_inventing_status_text(monkeypatch):
    sent: list[dict] = []
    edited: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [200],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        or True,
    )

    orch = SimpleNamespace(
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    run_id = _create_active_run()
    now = datetime.now(timezone.utc)

    gateway._notify_progress(
        42,
        ProgressUpdate(
            run_id=run_id,
            event_type="start",
            phase="starting",
            human_summary="AI Tech Lead started.",
            occurred_at=now,
        ),
    )
    gateway._notify_progress(
        42,
        ProgressUpdate(
            run_id=run_id,
            event_type="phase",
            phase="editing",
            human_summary="Applying the requested change.",
            occurred_at=now + timedelta(seconds=5),
            sequence=1,
        ),
    )
    gateway._notify_progress(
        42,
        ProgressUpdate(
            run_id=run_id,
            event_type="heartbeat",
            phase="editing",
            human_summary="Still working: editing.",
            occurred_at=now + timedelta(seconds=90),
        ),
    )

    assert len(sent) == 1
    assert edited[-1] == {
        "chat_id": 42,
        "message_id": 200,
        "text": "AI Tech Lead is working\n\nApplying the requested change.\n\nElapsed: 1 minute",
        "parse_mode": None,
    }
    assert "Still working" not in edited[-1]["text"]


@pytest.mark.parametrize(
    "reply_text",
    [
        "[AI Tech Lead] Clarification needed: Which repo should I change?",
        "[AI Tech Lead] Approval required: Need permission to apply the patch.\nUse /approve to continue or /reject <reason> to stop.",
        "[AI Tech Lead] Done.",
    ],
)
def test_progress_and_terminal_operator_messages_stay_separate(monkeypatch, reply_text):
    sent: list[dict] = []
    edited: list[dict] = []
    next_message_id = 300

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [next_message_id + len(sent) - 1],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        or True,
    )

    now = datetime.now(timezone.utc)

    def _invoke(message, *, progress_notify=None):
        run_id = _create_active_run()
        assert progress_notify is not None
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="start",
                phase="starting",
                human_summary="AI Tech Lead started.",
                occurred_at=now,
            )
        )
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="phase",
                phase="editing",
                human_summary="Applying the requested change.",
                occurred_at=now + timedelta(seconds=10),
                sequence=1,
            )
        )
        return reply_text

    orch = SimpleNamespace(
        pending_run=lambda: None,
        invoke=_invoke,
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._process_user_message(42, "Ship it")

    assert sent[0]["text"] == "AI Tech Lead is working\n\nElapsed: under 1 minute"
    assert edited == [
        {
            "chat_id": 42,
            "message_id": 300,
            "text": "AI Tech Lead is working\n\nApplying the requested change.\n\nElapsed: under 1 minute",
        }
    ]
    assert sent[-1]["text"] == reply_text
    assert len(sent) == 2


def test_failure_sends_only_one_final_failure_message(monkeypatch):
    sent: list[dict] = []
    edited: list[dict] = []
    next_message_id = 400

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [next_message_id + len(sent) - 1],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        or True,
    )

    now = datetime.now(timezone.utc)

    def _invoke(message, *, progress_notify=None):
        run_id = _create_active_run()
        assert progress_notify is not None
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="start",
                phase="starting",
                human_summary="AI Tech Lead started.",
                occurred_at=now,
            )
        )
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="phase",
                phase="editing",
                human_summary="Applying the requested change.",
                occurred_at=now + timedelta(seconds=5),
                sequence=1,
            )
        )
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="failure",
                phase="editing",
                human_summary="Agent failed.",
                occurred_at=now + timedelta(seconds=6),
                sequence=2,
            )
        )
        return "[AI Tech Lead] Failed: Patch conflicted. Resolve the conflict and retry."

    orch = SimpleNamespace(
        pending_run=lambda: None,
        invoke=_invoke,
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)

    gateway._process_user_message(42, "Ship it")

    assert len(sent) == 2
    assert edited == [
        {
            "chat_id": 42,
            "message_id": 400,
            "text": "AI Tech Lead is working\n\nApplying the requested change.\n\nElapsed: under 1 minute",
        }
    ]
    assert [item["text"] for item in sent].count(
        "[AI Tech Lead] Failed: Patch conflicted. Resolve the conflict and retry."
    ) == 1


def test_concurrent_runs_keep_separate_live_message_ids(monkeypatch):
    sent: list[dict] = []
    edited: list[dict] = []
    next_message_id = 500

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [next_message_id + len(sent) - 1],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        or True,
    )

    orch = SimpleNamespace(
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    now = datetime.now(timezone.utc)
    run_a = _create_active_run()
    run_b = _create_active_run()

    gateway._notify_progress(
        42,
        ProgressUpdate(
            run_id=run_a,
            event_type="start",
            phase="starting",
            human_summary="AI Tech Lead started.",
            occurred_at=now,
        ),
    )
    gateway._notify_progress(
        77,
        ProgressUpdate(
            run_id=run_b,
            event_type="start",
            phase="starting",
            human_summary="AI Tech Lead started.",
            occurred_at=now,
        ),
    )
    gateway._notify_progress(
        42,
        ProgressUpdate(
            run_id=run_a,
            event_type="phase",
            phase="editing",
            human_summary="Applying change A.",
            occurred_at=now + timedelta(seconds=10),
            sequence=1,
        ),
    )
    gateway._notify_progress(
        77,
        ProgressUpdate(
            run_id=run_b,
            event_type="phase",
            phase="editing",
            human_summary="Applying change B.",
            occurred_at=now + timedelta(seconds=10),
            sequence=1,
        ),
    )

    assert [item["chat_id"] for item in sent] == [42, 77]
    assert edited == [
        {
            "chat_id": 42,
            "message_id": 500,
            "text": "AI Tech Lead is working\n\nApplying change A.\n\nElapsed: under 1 minute",
        },
        {
            "chat_id": 77,
            "message_id": 501,
            "text": "AI Tech Lead is working\n\nApplying change B.\n\nElapsed: under 1 minute",
        },
    ]


def test_run_telegram_refuses_a_second_instance(monkeypatch, tmp_path):
    """Two Telegram gateway processes on the same bot token both poll and both
    reply to every message — this is the exact bug that motivated the lock.
    A second run_telegram() call while one is "running" must be refused."""
    monkeypatch.setattr(singleton_lock, "SINGLETON_LOCKS_DIR", tmp_path / "runtime_locks")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)

    first_lock = singleton_lock.acquire_singleton_lock("telegram-gateway")
    try:
        with pytest.raises(SingletonLockBusyError, match="telegram-gateway"):
            run_telegram()
    finally:
        first_lock.release()


def test_run_telegram_releases_the_lock_after_it_exits(monkeypatch, tmp_path):
    monkeypatch.setattr(singleton_lock, "SINGLETON_LOCKS_DIR", tmp_path / "runtime_locks")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        "agent_hub.startup_health.ensure_healthy_startup", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway.HubOrchestrator", lambda: SimpleNamespace()
    )
    monkeypatch.setattr(TelegramGateway, "__init__", lambda self, token, orch: None)
    monkeypatch.setattr(TelegramGateway, "run", lambda self, registry=None, **kwargs: None)
    monkeypatch.setenv("HUB_BOT_TOKEN", "token-123")

    run_telegram()

    lock_path = tmp_path / "runtime_locks" / "telegram-gateway.lock.json"
    assert not lock_path.exists()

    # And a fresh instance can start immediately afterward.
    second = singleton_lock.acquire_singleton_lock("telegram-gateway")
    second.release()


def test_waiting_progress_does_not_duplicate_terminal_decision_in_live_status(monkeypatch):
    sent: list[dict] = []
    edited: list[dict] = []

    monkeypatch.setattr(
        "agent_hub.telegram_gateway._send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [400 + len(sent) - 1],
    )
    monkeypatch.setattr(
        "agent_hub.telegram_gateway._edit_message",
        lambda token, chat_id, message_id, text, *, parse_mode=None: edited.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        or True,
    )

    now = datetime.now(timezone.utc)
    decision = "[AI Tech Lead] Decision needed: Choose a recovery action."

    def _invoke(message, *, progress_notify=None):
        run_id = _create_active_run()
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="start",
                phase="starting",
                human_summary="AI Tech Lead started.",
                occurred_at=now,
            )
        )
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="phase",
                phase="planning",
                human_summary="Preparing an implementation plan.",
                occurred_at=now + timedelta(seconds=5),
                sequence=1,
            )
        )
        progress_notify(
            ProgressUpdate(
                run_id=run_id,
                event_type="waiting",
                phase="waiting_decision",
                human_summary=decision,
                occurred_at=now + timedelta(seconds=6),
                sequence=2,
            )
        )
        return decision

    orch = SimpleNamespace(
        pending_run=lambda: None,
        invoke=_invoke,
        registry=[SimpleNamespace(id="ai-tech-lead", name="AI Tech Lead")],
        set_learning_notifier=lambda callback: None,
    )
    gateway = TelegramGateway("token-123", orch)
    gateway._process_user_message(42, "Do it")

    assert sent[-1]["text"] == decision
    assert sum(decision in item["text"] for item in sent) == 1
    assert all(decision not in item["text"] for item in edited)
