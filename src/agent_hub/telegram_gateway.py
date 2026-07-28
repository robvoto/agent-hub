"""Telegram polling bot gateway for the Agent Hub."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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


def _get_updates(token: str, offset: int) -> list[dict]:
    try:
        data = _api(token, "getUpdates", offset=offset, timeout=_POLL_TIMEOUT)
        return data.get("result", [])
    except Exception as exc:
        logger.warning("getUpdates failed: %s", exc)
        return []


def _send_message(
    token: str,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = "Markdown",
) -> list[int]:
    sent_ids: list[int] = []
    try:
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            result = _api(token, "sendMessage", **payload).get("result", {})
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                sent_ids.append(message_id)
        human_logger.info("Telegram reply to chat %d: %s", chat_id, _truncate(text))
    except Exception as exc:
        logger.error("sendMessage failed: %s", exc)
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
        logger.error("editMessageText failed: %s", exc)
        return False
    except Exception as exc:
        logger.error("editMessageText failed: %s", exc)
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


class TelegramGateway:
    def __init__(self, token: str, orchestrator: HubOrchestrator) -> None:
        self._token = token
        self._orch = orchestrator
        self._allowed = _allowed_chat_ids()
        self._workers_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._last_chat_id: int | None = None
        self._seen_lock = threading.Lock()
        self._recent_update_ids: deque[int] = deque(maxlen=256)
        self._recent_message_keys: deque[tuple[int, int]] = deque(maxlen=256)
        self._progress_lock = threading.Lock()
        self._live_progress_messages: dict[str, _LiveProgressMessage] = {}
        self._orch.set_learning_notifier(self._notify_learning)

    def _notify_learning(self, message: str) -> None:
        if self._last_chat_id is None:
            return
        _send_message(self._token, self._last_chat_id, message, parse_mode=None)

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
            "Send a plain message to dispatch it to a specialist agent "
            "(e.g. AI Tech Lead). Slash commands control the hub itself:\n\n"
            "/agents - list registered specialist agents\n"
            "/agents-refresh - re-read the specialist registry now and show what changed\n"
            "/agents-status - show registry health (versions, fingerprints, invalid "
            "manifests) without refreshing\n"
            "/approve - approve a task waiting on approval\n"
            "/forget <id> - remove a stored learning\n"
            "/help - show this\n"
            "/hub-status - show the hub startup summary without starting a new "
            "conversation\n"
            "/last - show the most recently finished task\n"
            "/learn <fact> - store an explicit learning and get a recommended next action\n"
            "/learn-mode [on|off] - toggle automatic background learning "
            "(off by default; shows status with no argument)\n"
            "/memory - list stored learnings\n"
            "/new - start a fresh conversation; keep active work running\n"
            "/project [<path>|clear] - set/show/clear the target project "
            "passed to specialists (shows current with no argument)\n"
            "/reject [reason] - reject a task waiting on approval\n"
            "/reset - stop the active specialist tree here, then start a fresh conversation\n"
            "/status - show the active or paused task\n"
            "/stop - cancel the active task and its specialist tree; keep this conversation\n"
            "\n"
            "Thread model:\n"
            "Reply normally to continue a clarification pause in the same thread.\n"
            "Use /approve to continue an approval pause in the same thread.\n"
            "Reply with the option number or name to continue a decision pause; /status shows\n"
            "the options a paused specialist last reported.\n"
            "/new starts a fresh empty thread; it is not a fork.\n"
            "Cancelled work from /stop or /reset is not resumable.\n"
            "There is no /fork or generic /resume command yet.\n"
        )

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if not self._is_allowed(chat_id):
            logger.info("Ignored message from unauthorized chat %d", chat_id)
            return

        self._last_chat_id = chat_id
        human_logger.info("Telegram message from chat %d: %s", chat_id, _truncate(text))

        if text == "/help":
            _send_message(self._token, chat_id, self._help_text(), parse_mode=None)
            return

        if text == "/new":
            summary = self._orch.new_session()
            with self._progress_lock:
                self._live_progress_messages.clear()
            _send_message(self._token, chat_id, summary, parse_mode=None)
            return

        if text == "/hub-status":
            _send_message(self._token, chat_id, self._orch.hub_status(), parse_mode=None)
            return

        if text == "/reset":
            reply = self._orch.reset_session()
            with self._progress_lock:
                self._live_progress_messages.clear()
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/agents":
            specs = self._orch.registry
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
                self._orch.refresh_registry(),
                parse_mode=None,
            )
            return

        if text == "/agents-status":
            _send_message(
                self._token,
                chat_id,
                self._orch.agents_status(),
                parse_mode=None,
            )
            return

        if text == "/status":
            _send_message(
                self._token,
                chat_id,
                self._orch.current_run_status(),
                parse_mode=None,
            )
            return

        if text == "/last":
            _send_message(
                self._token,
                chat_id,
                self._orch.last_run_status(),
                parse_mode=None,
            )
            return

        if text == "/learn" or text.startswith("/learn "):
            value = text[len("/learn"):].strip()
            reply = (
                "Usage: /learn <instruction or fact>"
                if not value
                else self._orch.learn(value, source=f"telegram chat {chat_id}")
            )
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/memory":
            _send_message(self._token, chat_id, self._orch.memory(), parse_mode=None)
            return

        if text.startswith("/learn-mode"):
            arg = text[len("/learn-mode"):].strip().lower()
            if arg == "on":
                reply = self._orch.set_learning_mode(True)
            elif arg == "off":
                reply = self._orch.set_learning_mode(False)
            elif not arg:
                reply = self._orch.learning_mode_status()
            else:
                reply = "Usage: /learn-mode [on|off]"
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text.startswith("/forget"):
            identifier = text[len("/forget"):].strip()
            _send_message(
                self._token,
                chat_id,
                self._orch.forget_learning(identifier),
                parse_mode=None,
            )
            return

        if text.startswith("/project"):
            arg = text[len("/project"):].strip()
            if not arg:
                reply = self._orch.current_project_status()
            elif arg.lower() == "clear":
                reply = self._orch.clear_current_project()
            else:
                reply = self._orch.set_current_project(arg)
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/stop":
            reply = self._orch.stop_current_task()
            _send_message(self._token, chat_id, reply, parse_mode=None)
            return

        if text == "/approve":
            try:
                reply = self._orch.approve_pending(
                    progress_notify=lambda update: self._notify_progress(chat_id, update)
                )
            except Exception as exc:
                logger.exception("Approval resume error")
                reply = f"Error: {exc}"
            _send_message(self._token, chat_id, reply)
            return

        if text.startswith("/reject"):
            reason = text[len("/reject"):].strip() or "Rejected by user"
            try:
                reply = self._orch.reject_pending(reason)
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
        worker = threading.Thread(
            target=self._process_user_message,
            args=(chat_id, text),
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
                "Skipping %d queued Telegram update(s) older than %ds on startup; "
                "next offset=%d",
                len(stale),
                int(_STARTUP_STALE_SECONDS),
                next_offset,
            )

        for update in fresh:
            self._handle_update(update)

        return next_offset

    def _process_user_message(self, chat_id: int, text: str) -> None:
        try:
            pending = self._orch.pending_run()
            if pending is not None and pending.state == "waiting_decision":
                reply = self._orch.provide_decision_reply(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                )
            elif pending is not None and pending.state == "waiting_clarification":
                reply = self._orch.provide_clarification(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                )
            else:
                reply = self._orch.invoke(
                    text,
                    progress_notify=lambda update: self._notify_progress(chat_id, update),
                )
        except TaskCancelled:
            logger.info("Task was cancelled before completion message delivery.")
            return
        except Exception as exc:
            logger.exception("Orchestrator error")
            reply = f"Error: {exc}"

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
            "Hub Telegram gateway starting (pid=%d, session %s, api=%s).",
            os.getpid(),
            self._orch.session_id,
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
        if update.event_type in {"heartbeat", "start", "failure"}:
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
        elapsed = rendered_at - run.created_at.astimezone(timezone.utc)
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
            for spec in self._orch.registry:
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
            return f"{hours} hour{'s' if hours != 1 else ''} {minutes} minute{'s' if minutes != 1 else ''}"
        if hours:
            return f"{hours} hour{'s' if hours != 1 else ''}"
        if minutes < 1:
            return "under 1 minute"
        return f"{minutes} minute{'s' if minutes != 1 else ''}"


def run_telegram(token: str | None = None) -> None:
    from dotenv import load_dotenv

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
        gateway.run()
    finally:
        lock.release()
