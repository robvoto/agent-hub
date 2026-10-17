"""Telegram polling bot gateway for the Agent Hub."""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from .log_config import get_human_logger
from .orchestrator import HubOrchestrator, cancel_all_active_tasks
from .progress_events import ProgressUpdate
from .task_control import TaskCancelled
from .task_runs import get_task_run_store

logger = logging.getLogger(__name__)
human_logger = get_human_logger()

_POLL_TIMEOUT = 30
_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
# Startup only: updates already queued when Hub was down longer than this are treated
# as stale backlog and skipped. Anything newer (e.g. a message sent right as Hub was
# restarting) is processed normally instead of silently dropped.
_STARTUP_STALE_SECONDS = 60.0
_LIVE_PROGRESS_REFRESH_SECONDS = 75.0


@dataclass
class _LiveProgressMessage:
    chat_id: int
    message_id: int | None
    summary: str | None
    rendered_text: str
    last_rendered_at: datetime


def _raise_keyboard_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt()


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


def _update_timestamp(update: dict) -> float | None:
    msg = update.get("message") or update.get("edited_message")
    date = msg.get("date") if isinstance(msg, dict) else None
    return float(date) if isinstance(date, (int, float)) else None


def _api(token: str, method: str, **kwargs: Any) -> dict:
    url = f"{_API_BASE}/bot{token}/{method}"
    resp = httpx.post(url, json=kwargs, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _safe_transport_error(exc: Exception, token: str) -> str:
    """Render a transport exception without ever logging the bot token."""
    rendered = str(exc)
    if token:
        rendered = rendered.replace(token, "<redacted-telegram-bot-token>")
    return rendered


def _get_updates(token: str, offset: int) -> list[dict]:
    try:
        data = _api(token, "getUpdates", offset=offset, timeout=_POLL_TIMEOUT)
        return data.get("result", [])
    except Exception as exc:
        logger.warning("getUpdates failed: %s", _safe_transport_error(exc, token))
        return []


def _send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = "Markdown",
    log_to_human: bool = True,
) -> list[int]:
    sent_ids: list[int] = []
    chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
    for chunk in chunks:
        try:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            try:
                result = _api(token, "sendMessage", **payload).get("result", {})
            except httpx.HTTPStatusError as exc:
                if parse_mode is None or exc.response is None or exc.response.status_code != 400:
                    raise
                logger.warning(
                    "Telegram rejected formatted message; retrying chunk as plain text."
                )
                result = _api(
                    token,
                    "sendMessage",
                    chat_id=chat_id,
                    text=chunk,
                ).get("result", {})
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                sent_ids.append(message_id)
        except Exception as exc:
            logger.error("sendMessage failed: %s", _safe_transport_error(exc, token))
            break
    if sent_ids:
        if log_to_human:
            human_logger.info("Telegram reply to chat %d: %s", chat_id, _truncate(text))
        else:
            # OpenCode outbox text is untrusted. Do not echo user/model-provided
            # task labels, reasons or evidence into the human-facing log.
            human_logger.info("Telegram advisory delivered to chat %d", chat_id)
    return sent_ids


def _edit_message(
    token: str,
    chat_id: int,
    message_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    try:
        _api(token, "editMessageText", **payload)
        # debug only, not human_logger: this is the same live-progress message
        # being refreshed in place (elapsed time, phase) — the human already
        # sees it live in Telegram, so repeating it in the human log is noise.
        logger.debug(
            "Telegram status edit in chat %d (message %d): %s",
            chat_id,
            message_id,
            _truncate(text),
        )
        return True
    except httpx.HTTPStatusError as exc:
        response_text = exc.response.text if exc.response is not None else ""
        if "message is not modified" in response_text:
            return False
        logger.error("editMessageText failed: %s", _safe_transport_error(exc, token))
        return False
    except Exception as exc:
        logger.error("editMessageText failed: %s", _safe_transport_error(exc, token))
        return False


def _allowed_chat_ids() -> set[int]:
    raw = os.getenv("HUB_ALLOWED_CHAT_IDS", "")
    if not raw.strip():
        return set()
    try:
        return {int(x.strip()) for x in raw.split(",") if x.strip()}
    except ValueError:
        logger.warning("Invalid HUB_ALLOWED_CHAT_IDS: %r", raw)
        return set()


def _telegram_session_scope(chat_id: int) -> str:
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:24]
    return f"telegram-{digest}"


def _resolve_primary_chat_id(allowed: set[int], primary_chat_id: int | None) -> int | None:
    configured = os.getenv("HUB_TELEGRAM_PRIMARY_CHAT_ID", "").strip()
    if primary_chat_id is None and configured:
        try:
            primary_chat_id = int(configured)
        except ValueError as exc:
            raise ValueError("HUB_TELEGRAM_PRIMARY_CHAT_ID must be an integer.") from exc
    elif primary_chat_id is None and len(allowed) == 1:
        primary_chat_id = next(iter(allowed))
    elif primary_chat_id is None and len(allowed) > 1:
        raise ValueError(
            "Set HUB_TELEGRAM_PRIMARY_CHAT_ID before allowing multiple Telegram chats."
        )

    if primary_chat_id is not None and allowed and primary_chat_id not in allowed:
        raise ValueError("HUB_TELEGRAM_PRIMARY_CHAT_ID must be in HUB_ALLOWED_CHAT_IDS.")
    return primary_chat_id


class TelegramGateway:
    def __init__(
        self,
        token: str,
        orchestrator: HubOrchestrator,
        *,
        primary_chat_id: int | None = None,
        orchestrator_factory: Callable[[int], HubOrchestrator] | None = None,
    ) -> None:
        self._token = token
        self._allowed = _allowed_chat_ids()
        self._primary_chat_id = _resolve_primary_chat_id(self._allowed, primary_chat_id)
        self._orchestrator_factory = orchestrator_factory or self._create_chat_orchestrator
        self._orchestrator_lock = threading.RLock()
        self._orchestrators: dict[int, HubOrchestrator] = {}
        self._unassigned_orchestrator = orchestrator if self._primary_chat_id is None else None
        if self._primary_chat_id is not None:
            self._orchestrators[self._primary_chat_id] = orchestrator
            self._bind_learning_notifier(self._primary_chat_id, orchestrator)
        self._workers_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._seen_lock = threading.Lock()
        self._recent_update_ids: deque[int] = deque(maxlen=256)
        self._recent_message_keys: deque[tuple[int, int]] = deque(maxlen=256)
        self._progress_lock = threading.Lock()
        self._live_progress_messages: dict[str, _LiveProgressMessage] = {}
        self._notifications: Any | None = None

    def set_notifications(self, notifications: Any | None) -> None:
        """Attach an opt-in notification bridge polled by :meth:`run`.

        The bridge only needs a ``poll_once() -> int`` method. It is polled
        once per gateway loop iteration so it shares the existing process and
        operator channel; a bridge error never stops the gateway.
        """
        self._notifications = notifications

    def notify_operator(self, text: str) -> bool:
        """Send a Hub-originated notification to the primary operator chat.

        Uses the existing bot token and ``_send_message`` path with the
        resolved primary chat, so only the configured/allow-listed chat can be
        targeted. Returns ``False`` when no primary chat is configured.
        """
        if self._primary_chat_id is None:
            return False
        return bool(
            _send_message(
                self._token,
                self._primary_chat_id,
                text,
                parse_mode=None,
                log_to_human=False,
            )
        )

    def _poll_notifications(self) -> None:
        if self._notifications is None:
            return
        try:
            self._notifications.poll_once()
        except Exception as exc:  # noqa: BLE001 - a bridge fault must not stop Telegram
            logger.warning("OpenCode notification poll failed (%s).", type(exc).__name__)

    def _create_chat_orchestrator(self, chat_id: int) -> HubOrchestrator:
        return HubOrchestrator(session_scope=_telegram_session_scope(chat_id))

    def _bind_learning_notifier(self, chat_id: int, orchestrator: HubOrchestrator) -> None:
        orchestrator.set_learning_notifier(
            lambda message, chat_id=chat_id: self._notify_learning(chat_id, message)
        )

    def _orchestrator_for_chat(self, chat_id: int) -> HubOrchestrator:
        with self._orchestrator_lock:
            existing = self._orchestrators.get(chat_id)
            if existing is not None:
                return existing

            if self._unassigned_orchestrator is not None:
                orchestrator = self._unassigned_orchestrator
                self._unassigned_orchestrator = None
            else:
                orchestrator = self._orchestrator_factory(chat_id)
            self._orchestrators[chat_id] = orchestrator
            self._bind_learning_notifier(chat_id, orchestrator)
            return orchestrator

    def _notify_learning(self, chat_id: int, message: str) -> None:
        _send_message(self._token, chat_id, message, parse_mode=None)

    def _clear_live_progress(self, chat_id: int) -> None:
        with self._progress_lock:
            self._live_progress_messages = {
                run_id: progress
                for run_id, progress in self._live_progress_messages.items()
                if progress.chat_id != chat_id
            }

    def _is_allowed(self, chat_id: int) -> bool:
        return not self._allowed or chat_id in self._allowed

    def _is_duplicate_update(self, update: dict) -> bool:
        update_id = update.get("update_id")
        msg = update.get("message") or update.get("edited_message")
        chat_id = msg.get("chat", {}).get("id") if isinstance(msg, dict) else None
        message_id = msg.get("message_id") if isinstance(msg, dict) else None

        with self._seen_lock:
            if isinstance(update_id, int) and update_id in self._recent_update_ids:
                human_logger.info("Ignoring duplicate Telegram update %d.", update_id)
                return True

            if (
                isinstance(chat_id, int)
                and isinstance(message_id, int)
                and (chat_id, message_id) in self._recent_message_keys
            ):
                human_logger.info(
                    "Ignoring duplicate Telegram message %d from chat %d.",
                    message_id,
                    chat_id,
                )
                if isinstance(update_id, int):
                    self._recent_update_ids.append(update_id)
                return True

            if isinstance(update_id, int):
                self._recent_update_ids.append(update_id)
            if isinstance(chat_id, int) and isinstance(message_id, int):
                self._recent_message_keys.append((chat_id, message_id))
        return False

    @staticmethod
    def _help_text() -> str:
        return (
            "Agent Hub\n"
            "Send a plain message to dispatch work. Commands:\n\n"
            "/new - start a fresh conversation; paused work is preserved\n"
            "/tasks - list all active/paused tasks\n"
            "/resume <id> - select a paused task to continue\n"
            "/factory-adopt <thread-id> <BUILD_TASK reference> - review one approved Factory task\n"
            "/status - show details for this conversation's current task\n"
            "/stop [id] - cancel the current task, or a specific task\n"
            "/reset - cancel the current task and start a fresh conversation\n"
            "/reset-all - cancel all active/paused tasks and start fresh\n"
            "/approve - approve the current task when requested\n"
            "/reject [reason] - reject the current task when requested\n"
            "/project [<path>|clear] - set/show/clear the target project\n"
            "/hub-status - show Hub status\n"
            "/last - show the most recently finished task\n"
            "/agents - list registered specialists\n"
            "/agents-refresh - refresh specialist registry\n"
            "/agents-status - show specialist registry health\n"
            "/learn <fact> - store an explicit learning\n"
            "/learn-mode [on|off] - set/show persistent automatic learning preference\n"
            "/memory - list stored learnings\n"
            "/forget <id> - remove a stored learning\n"
            "/help - show this\n"
        )

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        raw_text = msg.get("text", "")
        if not isinstance(raw_text, str):
            return
        text = raw_text.strip()

        if not self._is_allowed(chat_id):
            logger.info("Ignored message from unauthorized chat %d", chat_id)
            return

        human_logger.info("Telegram message from chat %d: %s", chat_id, _truncate(text))

        if text == "/help":
            _send_message(self._token, chat_id, self._help_text(), parse_mode=None)
            return

        orch = self._orchestrator_for_chat(chat_id)

        if text == "/new":
            summary = orch.new_session()
            self._clear_live_progress(chat_id)
            _send_message(self._token, chat_id, summary, parse_mode=None)
            return

        if text == "/hub-status":
            _send_message(self._token, chat_id, orch.hub_status(), parse_mode=None)
            return

        if text == "/reset":
            reply = orch.reset_session()
            self._clear_live_progress(chat_id)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/reset-all":
            reply = orch.reset_all()
            self._clear_live_progress(chat_id)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/agents":
            specs = orch.registry
            if not specs:
                reply = "No agents registered yet."
            else:
                lines = [f"*{s.name}* (`{s.id}`): {s.purpose}" for s in specs]
                reply = "**Registered agents:**\n" + "\n".join(lines)
            _send_message(self._token, chat_id, reply)
            return

        if text == "/agents-refresh":
            _send_message(
                self._token,
                chat_id,
                orch.refresh_registry(),
                parse_mode=None,
            )
            return

        if text == "/agents-status":
            _send_message(
                self._token,
                chat_id,
                orch.agents_status(),
                parse_mode=None,
            )
            return

        if text == "/status":
            _send_message(
                self._token,
                chat_id,
                orch.current_run_status(),
                parse_mode=None,
            )
            return

        if text == "/tasks":
            _send_message(self._token, chat_id, orch.tasks_status(), parse_mode=None)
            return

        if text == "/resume" or text.startswith("/resume "):
            identifier = text[len("/resume") :].strip()
            reply = "Usage: /resume <id>" if not identifier else orch.resume_task(identifier)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/factory-adopt" or text.startswith("/factory-adopt "):
            parts = text.split()
            reply = (
                "Usage: /factory-adopt <Factory-thread-id> <BUILD_TASK reference>"
                if len(parts) != 3
                else orch.adopt_factory_task(parts[1], parts[2])
            )
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/last":
            _send_message(
                self._token,
                chat_id,
                orch.last_run_status(),
                parse_mode=None,
            )
            return

        if text == "/learn" or text.startswith("/learn "):
            value = text[len("/learn") :].strip()
            reply = (
                "Usage: /learn <instruction or fact>"
                if not value
                else orch.learn(value, source=f"telegram chat {chat_id}")
            )
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/memory":
            _send_message(self._token, chat_id, orch.memory(), parse_mode=None)
            return

        if text.startswith("/learn-mode"):
            arg = text[len("/learn-mode") :].strip().lower()
            if arg == "on":
                reply = orch.set_learning_mode(True)
            elif arg == "off":
                reply = orch.set_learning_mode(False)
            elif not arg:
                reply = orch.learning_mode_status()
            else:
                reply = "Usage: /learn-mode [on|off]"
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text.startswith("/forget"):
            identifier = text[len("/forget") :].strip()
            _send_message(
                self._token,
                chat_id,
                orch.forget_learning(identifier),
                parse_mode=None,
            )
            return

        if text.startswith("/project"):
            arg = text[len("/project") :].strip()
            if not arg:
                reply = orch.current_project_status()
            elif arg.lower() == "clear":
                reply = orch.clear_current_project()
            else:
                reply = orch.set_current_project(arg)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/stop" or text.startswith("/stop "):
            identifier = text[len("/stop") :].strip() or None
            reply = orch.stop_current_task(identifier=identifier)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/approve":
            try:
                reply = orch.approve_pending(
                    progress_notify=lambda update: self._notify_progress(chat_id, update)
                )
            except Exception as exc:
                logger.exception("Approval resume error")
                reply = f"Error: {exc}"
            _send_message(self._token, chat_id, reply)
            return

        if text.startswith("/reject"):
            reason = text[len("/reject") :].strip() or "Rejected by user"
            try:
                reply = orch.reject_pending(reason)
            except Exception as exc:
                logger.exception("Approval rejection error")
                reply = f"Error: {exc}"
            _send_message(self._token, chat_id, reply)
            return

        if not text or text.startswith("/"):
            return

        # Multiple projects can run concurrently — HubOrchestrator.invoke()
        # itself rejects a new task for a project that already has one
        # in flight, so the gateway just dispatches every message and lets
        # that per-project check produce the "already running" reply.
        request_started_at = datetime.now(timezone.utc)
        worker = threading.Thread(
            target=self._process_user_message,
            args=(chat_id, raw_text, request_started_at),
            daemon=True,
        )
        with self._workers_lock:
            self._workers = [w for w in self._workers if w.is_alive()]
            self._workers.append(worker)
            active_count = len(self._workers)
        human_logger.info(
            "Starting worker for chat %d (%d worker(s) now active)", chat_id, active_count
        )
        worker.start()

    def _prime_offset(self) -> int:
        updates = _get_updates(self._token, 0)
        if not updates:
            return 0

        next_offset = max(int(update["update_id"]) for update in updates) + 1

        now = time.time()
        stale = []
        fresh = []
        for update in updates:
            timestamp = _update_timestamp(update)
            if timestamp is not None and now - timestamp > _STARTUP_STALE_SECONDS:
                stale.append(update)
            else:
                fresh.append(update)

        if stale:
            human_logger.info(
                "Skipping %d queued Telegram update(s) older than %ds on startup; next offset=%d",
                len(stale),
                int(_STARTUP_STALE_SECONDS),
                next_offset,
            )

        for update in fresh:
            self._handle_update(update)

        return next_offset

    def _process_user_message(
        self, chat_id: int, text: str, request_started_at: datetime | None = None
    ) -> None:
        orch = self._orchestrator_for_chat(chat_id)
        try:
            pending = orch.pending_run()
            if pending is not None and pending.state == "waiting_decision":
                reply = orch.provide_decision_reply(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                )
            elif pending is not None and pending.state == "waiting_clarification":
                reply = orch.provide_clarification(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                )
            else:
                reply = orch.invoke(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                    request_started_at=request_started_at,
                )
        except TaskCancelled:
            logger.info("Task was cancelled before completion message delivery.")
            return
        except Exception as exc:
            logger.exception("Orchestrator error")
            reply = f"Error: {exc}"
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        _send_message(self._token, chat_id, reply)

    def _notify_progress(self, chat_id: int, update: ProgressUpdate) -> None:
        run = get_task_run_store().get_run(update.run_id)
        if run is None:
            return

        summary = self._meaningful_specialist_summary(run, update)
        rendered_at = update.occurred_at.astimezone(timezone.utc)

        with self._progress_lock:
            current = self._live_progress_messages.get(update.run_id)
            if current is None:
                text = self._render_live_progress(run, summary, rendered_at=rendered_at)
                sent_ids = _send_message(self._token, chat_id, text, parse_mode=None)
                message_id = sent_ids[-1] if sent_ids else None
                self._live_progress_messages[update.run_id] = _LiveProgressMessage(
                    chat_id=chat_id,
                    message_id=message_id,
                    summary=summary,
                    rendered_text=text,
                    last_rendered_at=rendered_at,
                )
                if message_id is not None:
                    get_task_run_store().update_context(
                        update.run_id,
                        telegram_live_chat_id=chat_id,
                        telegram_live_message_id=message_id,
                    )
                return

            refresh_due = (
                rendered_at - current.last_rendered_at
            ).total_seconds() >= _LIVE_PROGRESS_REFRESH_SECONDS
            next_summary = current.summary
            if summary is not None and summary != current.summary:
                next_summary = summary
            elif not refresh_due:
                return

            text = self._render_live_progress(run, next_summary, rendered_at=rendered_at)
            if text == current.rendered_text:
                return
            if current.message_id is None:
                return
            if not _edit_message(
                self._token,
                current.chat_id,
                current.message_id,
                text,
                parse_mode=None,
            ):
                return
            self._live_progress_messages[update.run_id] = _LiveProgressMessage(
                chat_id=current.chat_id,
                message_id=current.message_id,
                summary=next_summary,
                rendered_text=text,
                last_rendered_at=rendered_at,
            )

    def _handle_update(self, update: dict) -> None:
        if self._is_duplicate_update(update):
            return
        msg = update.get("message") or update.get("edited_message")
        if msg:
            self._handle_message(msg)

    def run(self) -> None:
        human_logger.info(
            "Hub Telegram gateway starting (pid=%d, api=%s).",
            os.getpid(),
            _API_BASE,
        )
        logger.info(
            "Hub Telegram gateway starting (pid=%d, primary chat configured=%s, api=%s).",
            os.getpid(),
            self._primary_chat_id is not None,
            _API_BASE,
        )
        previous_sigterm_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        offset = self._prime_offset()
        try:
            while True:
                updates = _get_updates(self._token, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    self._handle_update(update)
                self._poll_notifications()
                if not updates:
                    time.sleep(1)
        except KeyboardInterrupt:
            self._shutdown()
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)

    def _shutdown(self) -> None:
        cancelled = cancel_all_active_tasks("Hub Telegram gateway shut down.")
        if cancelled:
            human_logger.info(
                "Cancelled %d in-flight task(s) on shutdown: %s",
                len(cancelled),
                ", ".join(run_id[:8] for run_id in cancelled),
            )
        human_logger.info("Hub Telegram gateway stopped by user (Ctrl-C).")

    def _meaningful_specialist_summary(self, run: Any, update: ProgressUpdate) -> str | None:
        # Terminal/action-required events are delivered separately as the operator
        # message. Do not copy their full payload into the live progress message,
        # otherwise Telegram shows the same decision/clarification twice.
        if update.event_type in {"heartbeat", "start", "waiting", "completed", "failure"}:
            return None
        summary = " ".join(update.human_summary.split())
        if not summary:
            return None
        agent_name = self._agent_display_name(run).lower()
        phase = " ".join((update.phase or "").split()).strip(" .:").lower()
        normalized = summary.strip(" .:").lower()
        if normalized in {"in progress", "processing", "running", "working"}:
            return None
        if normalized.startswith("still working"):
            return None
        if normalized == f"{agent_name} is still working":
            return None
        if phase and normalized == phase:
            return None
        return summary

    def _render_live_progress(
        self,
        run: Any,
        summary: str | None,
        *,
        rendered_at: datetime,
    ) -> str:
        lines = [f"{self._agent_display_name(run)} is working", ""]
        if summary:
            lines.extend([summary, ""])
        lifecycle_started_at = run.created_at.astimezone(timezone.utc)
        stored_started_at = run.context.get("request_started_at")
        if isinstance(stored_started_at, str):
            try:
                lifecycle_started_at = datetime.fromisoformat(stored_started_at).astimezone(
                    timezone.utc
                )
            except ValueError:
                logger.warning(
                    "Run %s has invalid request_started_at=%r; falling back to created_at.",
                    run.id[:8],
                    stored_started_at,
                )
        elapsed = rendered_at - lifecycle_started_at
        if elapsed.total_seconds() < 0:
            elapsed = timedelta(0)
        lines.append(f"Elapsed: {self._format_elapsed(elapsed.total_seconds())}")
        return "\n".join(lines)

    def _agent_display_name(self, run: Any) -> str:
        pinned = run.context.get("pinned_agent_spec")
        if isinstance(pinned, dict):
            name = pinned.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        agent_id = run.selected_agent_id
        if isinstance(agent_id, str) and agent_id:
            with self._orchestrator_lock:
                orchestrators = list(self._orchestrators.values())
                if self._unassigned_orchestrator is not None:
                    orchestrators.append(self._unassigned_orchestrator)
            for orchestrator in orchestrators:
                for spec in orchestrator.registry:
                    if spec.id == agent_id and spec.name.strip():
                        return spec.name.strip()
            return agent_id
        return "Specialist"

    @staticmethod
    def _format_elapsed(total_seconds: float) -> str:
        seconds = max(0, int(total_seconds))
        minutes, _ = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours and minutes:
            hour_unit = "hour" if hours == 1 else "hours"
            minute_unit = "minute" if minutes == 1 else "minutes"
            return f"{hours} {hour_unit} {minutes} {minute_unit}"
        if hours:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        if minutes < 1:
            return "under 1 minute"
        return f"{minutes} minute{'s' if minutes != 1 else ''}"


def run_telegram(token: str | None = None) -> None:
    from dotenv import load_dotenv

    from .opencode_notify import build_bridge_from_env
    from .singleton_lock import acquire_singleton_lock
    from .startup_health import ensure_healthy_startup

    load_dotenv()
    lock = acquire_singleton_lock("telegram-gateway")
    try:
        ensure_healthy_startup("telegram", telegram_token=token)
        tok = token or os.getenv("HUB_BOT_TOKEN")
        if not tok:
            raise RuntimeError("HUB_BOT_TOKEN is not set.")

        orch = HubOrchestrator()
        gateway = TelegramGateway(tok, orch)
        bridge = build_bridge_from_env(gateway.notify_operator)
        if bridge is not None:
            gateway.set_notifications(bridge)
            human_logger.info("OpenCode completion notifications enabled (opt-in).")
        gateway.run()
    finally:
        lock.release()
