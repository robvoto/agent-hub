"""Tests for the opt-in OpenCode -> Telegram notification bridge."""

from __future__ import annotations

import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

import agent_hub.opencode_notify as opencode_notify
import agent_hub.telegram_gateway as telegram_gateway
from agent_hub.config import ConfigurationError
from agent_hub.opencode_notify import (
    DEFAULT_NOTIFY_STATES,
    MAX_TEXT_CHARS,
    OpenCodeNotificationBridge,
    build_bridge_from_env,
    notifications_enabled,
    read_outbox,
)
from agent_hub.telegram_gateway import TelegramGateway

_SCHEMA = """
CREATE TABLE events (
    dedupe_key TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at REAL NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    delivered_at REAL
);
"""


def _row(
    event_id: str,
    state: str,
    *,
    created: float,
    task_id: str = "task-1",
    reason: str | None = "worker update",
    evidence: dict | None = None,
    payload_raw: str | None = None,
    severity: str = "info",
) -> tuple:
    payload = {
        "task_id": task_id,
        "session_id": "ses_test",
        "state": state,
        "previous_state": "RUNNING",
        "severity": severity,
        "reason": reason,
        "created_at": created,
        "repo": "MCP-server",
        "worktree": "/work/tree",
        "evidence": evidence or {},
    }
    payload_json = payload_raw if payload_raw is not None else json.dumps(payload)
    return (
        event_id,
        task_id,
        state,
        severity,
        created,
        payload_json,
        "delivered",
        1,
        None,
        created,
    )


def _make_outbox(tmp_path, rows, name: str = "supervisor.db"):
    path = tmp_path / name
    connection = sqlite3.connect(path)
    connection.execute(_SCHEMA)
    connection.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()
    return path


class _Recorder:
    """Fake operator send callable recording text, with optional failures."""

    def __init__(self, fail_times: int = 0, return_value: bool = True) -> None:
        self.texts: list[str] = []
        self.fail_times = fail_times
        self.return_value = return_value

    def __call__(self, text: str) -> bool:
        self.texts.append(text)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("transport down")
        return self.return_value


def _bridge(tmp_path, outbox, send, **kwargs) -> OpenCodeNotificationBridge:
    kwargs.setdefault("ledger_path", tmp_path / "ledger.jsonl")
    return OpenCodeNotificationBridge(outbox_db=outbox, send=send, **kwargs)


# --------------------------------------------------------------------------- #
# opt-in configuration
# --------------------------------------------------------------------------- #
def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(opencode_notify.ENABLED_ENV, raising=False)
    monkeypatch.delenv(opencode_notify.OUTBOX_ENV, raising=False)
    assert notifications_enabled() is False
    assert build_bridge_from_env(lambda text: True) is None


def test_enabled_without_outbox_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv(opencode_notify.ENABLED_ENV, "1")
    monkeypatch.delenv(opencode_notify.OUTBOX_ENV, raising=False)
    with pytest.raises(ConfigurationError):
        build_bridge_from_env(lambda text: True)


def test_enabled_builds_bridge(monkeypatch, tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    monkeypatch.setenv(opencode_notify.ENABLED_ENV, "true")
    monkeypatch.setenv(opencode_notify.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(opencode_notify.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    bridge = build_bridge_from_env(lambda text: True)
    assert bridge is not None
    assert bridge.poll_once() == 1


def test_unknown_state_filter_fails_closed(monkeypatch, tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    monkeypatch.setenv(opencode_notify.ENABLED_ENV, "1")
    monkeypatch.setenv(opencode_notify.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(opencode_notify.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(opencode_notify.STATES_ENV, "FAILED,BOGUS")
    with pytest.raises(ConfigurationError):
        build_bridge_from_env(lambda text: True)


def test_max_events_env_bounds(monkeypatch, tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    monkeypatch.setenv(opencode_notify.ENABLED_ENV, "1")
    monkeypatch.setenv(opencode_notify.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(opencode_notify.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(opencode_notify.MAX_EVENTS_ENV, "0")
    with pytest.raises(ConfigurationError):
        build_bridge_from_env(lambda text: True)


def test_custom_state_filter(monkeypatch, tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("e1", "RUNNING", created=1.0),
            _row("e2", "FAILED", created=2.0),
        ],
    )
    monkeypatch.setenv(opencode_notify.ENABLED_ENV, "1")
    monkeypatch.setenv(opencode_notify.OUTBOX_ENV, str(outbox))
    monkeypatch.setenv(opencode_notify.LEDGER_ENV, str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv(opencode_notify.STATES_ENV, "failed")
    send = _Recorder()
    bridge = build_bridge_from_env(send)
    assert bridge is not None
    assert bridge.poll_once() == 1
    assert "failed" in send.texts[0]


# --------------------------------------------------------------------------- #
# message content: advisory, no approvals, no false verification claims
# --------------------------------------------------------------------------- #
def test_permission_required_is_advisory_and_cannot_approve(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "BLOCKED_PERMISSION", created=1.0)])
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    text = send.texts[0]
    assert "permission required" in text
    assert "cannot approve it" in text
    assert "advisory" in text


def test_completion_reported_is_not_verified(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row(
                "e1",
                "DONE_REPORTED",
                created=1.0,
                evidence={"reported_tests": "76 passed", "reported_commit": "044b7e4"},
            )
        ],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    text = send.texts[0]
    assert "worker self-report" in text
    assert "not independently verified" in text
    assert "tests=76 passed" in text
    assert "commit=044b7e4" in text


def test_done_verified_is_advisory_not_cryptographic(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "DONE_VERIFIED", created=1.0)])
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    text = send.texts[0].lower()
    assert "advisory" in text
    assert "cryptograph" not in text


def test_failed_and_stalled_notifications(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("e1", "FAILED", created=1.0),
            _row("e2", "STALLED", created=2.0),
        ],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 2
    joined = "\n".join(send.texts)
    assert "failed" in joined
    assert "stalled" in joined


def test_running_is_not_notified_by_default(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "RUNNING", created=1.0)])
    send = _Recorder()
    bridge = _bridge(tmp_path, outbox, send)
    assert bridge.poll_once() == 0
    assert send.texts == []
    assert "RUNNING" not in DEFAULT_NOTIFY_STATES


# --------------------------------------------------------------------------- #
# malformed / bounded input
# --------------------------------------------------------------------------- #
def test_malformed_payload_is_skipped(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("bad", "FAILED", created=1.0, payload_raw="{not json"),
            _row("good", "FAILED", created=2.0),
        ],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    assert len(send.texts) == 1


def test_non_object_payload_is_skipped(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("list", "FAILED", created=1.0, payload_raw="[1, 2, 3]"),
            _row("good", "FAILED", created=2.0),
        ],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1


def test_very_long_field_is_bounded(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [_row("e1", "FAILED", created=1.0, reason="x" * 5000)],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    assert len(send.texts[0]) <= MAX_TEXT_CHARS


def test_unreadable_outbox_returns_zero(tmp_path) -> None:
    send = _Recorder()
    bridge = _bridge(tmp_path, tmp_path / "missing.db", send)
    assert bridge.poll_once() == 0
    assert bridge.last_error is not None
    assert send.texts == []


def test_no_secret_leaks_to_render_or_logs(tmp_path, caplog) -> None:
    reason_secret = "sk-live-REASON-CONFIDENTIAL"
    evidence_secret = "sk-live-EVIDENCE-CONFIDENTIAL"
    outbox = _make_outbox(
        tmp_path,
        [
            _row(
                "e1",
                "DONE_REPORTED",
                created=1.0,
                reason=f"token {reason_secret} rotated",
                evidence={"api_key": evidence_secret, "reported_tests": "passed"},
            )
        ],
    )
    send = _Recorder()
    with caplog.at_level(logging.DEBUG, logger="agent_hub.opencode_notify"):
        assert _bridge(tmp_path, outbox, send).poll_once() == 1
    # Unexpected evidence keys are never rendered into the notification.
    assert evidence_secret not in send.texts[0]
    # Neither secret may ever be written to the logs.
    assert all(reason_secret not in record.getMessage() for record in caplog.records)
    assert all(evidence_secret not in record.getMessage() for record in caplog.records)
    # Sanity: the safe evidence key is still surfaced.
    assert "tests=passed" in send.texts[0]


# --------------------------------------------------------------------------- #
# idempotency, restart and retry
# --------------------------------------------------------------------------- #
def test_duplicate_identity_is_not_resent_within_process(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    send = _Recorder()
    bridge = _bridge(tmp_path, outbox, send)
    assert bridge.poll_once() == 1
    assert bridge.poll_once() == 0
    assert len(send.texts) == 1


def test_restart_does_not_resend(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    assert _bridge(tmp_path, outbox, send).poll_once() == 0
    assert len(send.texts) == 1


def test_retry_after_transport_failure(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    send = _Recorder(fail_times=1)
    bridge = _bridge(tmp_path, outbox, send)
    assert bridge.poll_once() == 0  # raised -> cursor not advanced
    assert len(bridge.ledger) == 0
    assert bridge.poll_once() == 1  # retried
    assert len(bridge.ledger) == 1
    assert bridge.poll_once() == 0  # and not sent again
    assert len(send.texts) == 2  # one failed attempt + one success


def test_retry_when_send_returns_false(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    send = _Recorder(return_value=False)
    bridge = _bridge(tmp_path, outbox, send)
    assert bridge.poll_once() == 0
    assert len(bridge.ledger) == 0


def test_cursor_loss_does_not_resend(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("e1", "FAILED", created=1.0),
            _row("e2", "STALLED", created=2.0),
        ],
    )
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 2
    (tmp_path / "ledger.jsonl.cursor.json").unlink()
    assert _bridge(tmp_path, outbox, send).poll_once() == 0
    assert len(send.texts) == 2


def test_partial_ledger_line_is_tolerated(tmp_path) -> None:
    outbox = _make_outbox(
        tmp_path,
        [
            _row("e1", "FAILED", created=1.0, task_id="task-a"),
            _row("e2", "FAILED", created=2.0, task_id="task-b"),
        ],
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        '{"at": 1.0, "event_id": "e1"}\n{"at": 2.0, "event_id": "e2"',
        encoding="utf-8",
    )
    send = _Recorder()
    bridge = _bridge(tmp_path, outbox, send)
    # e1 is trusted from the complete line; e2's partial line is ignored so it
    # is re-delivered (documented at-least-once behaviour).
    assert bridge.poll_once() == 1
    assert "task-b" in send.texts[0]
    assert len(bridge.ledger) == 2


def test_corrupt_cursor_file_falls_back(tmp_path) -> None:
    outbox = _make_outbox(tmp_path, [_row("e1", "FAILED", created=1.0)])
    cursor_path = tmp_path / "ledger.jsonl.cursor.json"
    cursor_path.write_text("{this is not json", encoding="utf-8")
    send = _Recorder()
    assert _bridge(tmp_path, outbox, send).poll_once() == 1
    assert len(send.texts) == 1


# --------------------------------------------------------------------------- #
# bounded pagination: no event becomes permanently invisible
# --------------------------------------------------------------------------- #
def test_backlog_larger_than_batch_reaches_every_event(tmp_path, caplog) -> None:
    total = 120
    rows = [
        _row(f"e{i:03d}", "FAILED", created=float(i), task_id=f"task-{i:03d}")
        for i in range(total)
    ]
    outbox = _make_outbox(tmp_path, rows)
    send = _Recorder()
    with caplog.at_level(logging.WARNING, logger="agent_hub.opencode_notify"):
        delivered = 0
        # Simulate restarts between polls: a brand-new bridge each poll.
        for _ in range(10):
            batch = _bridge(tmp_path, outbox, send, max_events=50).poll_once()
            delivered += batch
            if batch == 0:
                break
    assert delivered == total
    assert len(send.texts) == total  # each event exactly once
    assert len(set(send.texts)) == total
    assert any("backlog exceeds" in record.getMessage() for record in caplog.records)


def test_oldest_events_are_not_starved_by_newer_ones(tmp_path) -> None:
    rows = [
        _row(f"e{i:03d}", "FAILED", created=float(i), task_id=f"task-{i:03d}")
        for i in range(60)
    ]
    outbox = _make_outbox(tmp_path, rows)
    send = _Recorder()
    bridge = _bridge(tmp_path, outbox, send, max_events=50)
    assert bridge.poll_once() == 50
    # The first batch must be the *oldest* events, not an arbitrary/newest slice.
    assert "task-000" in send.texts[0]
    assert bridge.poll_once() == 10
    assert "task-059" in send.texts[-1]


def test_read_outbox_reports_overflow(tmp_path) -> None:
    rows = [_row(f"e{i:03d}", "FAILED", created=float(i)) for i in range(5)]
    outbox = _make_outbox(tmp_path, rows)
    events, has_more = read_outbox(outbox, limit=3)
    assert [event.event_id for event in events] == ["e000", "e001", "e002"]
    assert has_more is True
    events, has_more = read_outbox(outbox, limit=3, after=(2.0, "e002"))
    assert [event.event_id for event in events] == ["e003", "e004"]
    assert has_more is False


# --------------------------------------------------------------------------- #
# gateway integration: plain-text send, allow-listed chat, non-fatal polling
# --------------------------------------------------------------------------- #
def _orch() -> SimpleNamespace:
    return SimpleNamespace(
        pending_run=lambda: None,
        invoke=lambda *a, **k: None,
        registry=[],
        set_learning_notifier=lambda callback: None,
    )


def test_notify_operator_uses_plain_text_and_primary_chat(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setenv("HUB_ALLOWED_CHAT_IDS", "42")
    monkeypatch.delenv("HUB_TELEGRAM_PRIMARY_CHAT_ID", raising=False)
    monkeypatch.setattr(
        telegram_gateway,
        "_send_message",
        lambda token, chat_id, text, *, parse_mode="Markdown": sent.append(
            {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
        )
        or [1],
    )
    gateway = TelegramGateway("token-123", _orch())
    assert gateway.notify_operator("hello operator") is True
    assert sent == [{"chat_id": 42, "text": "hello operator", "parse_mode": None}]


def test_notify_operator_without_primary_chat_returns_false(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.delenv("HUB_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("HUB_TELEGRAM_PRIMARY_CHAT_ID", raising=False)
    monkeypatch.setattr(
        telegram_gateway,
        "_send_message",
        lambda *a, **k: sent.append(1),
    )
    gateway = TelegramGateway("token-123", _orch())
    assert gateway.notify_operator("nobody") is False
    assert sent == []


def test_poll_notifications_swallows_bridge_errors(monkeypatch) -> None:
    monkeypatch.delenv("HUB_ALLOWED_CHAT_IDS", raising=False)
    gateway = TelegramGateway("token-123", _orch())

    class _Boom:
        def poll_once(self) -> int:
            raise RuntimeError("bridge exploded")

    gateway.set_notifications(_Boom())
    gateway._poll_notifications()  # must not raise


def test_run_polls_notifications_each_iteration(monkeypatch) -> None:
    monkeypatch.delenv("HUB_ALLOWED_CHAT_IDS", raising=False)
    gateway = TelegramGateway("token-123", _orch())
    polls: list[int] = []

    class _Counting:
        def poll_once(self) -> int:
            polls.append(1)
            return 0

    gateway.set_notifications(_Counting())
    gateway._shutdown = lambda: None  # type: ignore[method-assign]
    gateway._prime_offset = lambda: 0  # type: ignore[method-assign]

    calls = {"n": 0}

    def fake_updates(token: str, offset: int):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return []

    monkeypatch.setattr(telegram_gateway, "_get_updates", fake_updates)
    monkeypatch.setattr(telegram_gateway.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(telegram_gateway.signal, "signal", lambda *a, **k: None)

    gateway.run()
    assert polls  # the bridge was polled before shutdown
