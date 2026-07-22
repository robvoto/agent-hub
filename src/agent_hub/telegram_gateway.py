"""Telegram polling bot gateway for the Agent Hub."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from .orchestrator import HubOrchestrator
from .task_control import TaskCancelled

logger = logging.getLogger(__name__)

_POLL_TIMEOUT = 30
_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")


def _truncate(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}…"


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
) -> None:
    try:
        chunks = [text[i : i + 4096] for i in range(0, len(text), 4096)]
        for chunk in chunks:
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode is not None:
                payload["parse_mode"] = parse_mode
            _api(token, "sendMessage", **payload)
        logger.info("Reply to chat %d: %s", chat_id, _truncate(text))
    except Exception as exc:
        logger.error("sendMessage failed: %s", exc)


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
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._last_chat_id: int | None = None
        self._orch.set_learning_notifier(self._notify_learning)

    def _notify_learning(self, message: str) -> None:
        if self._last_chat_id is None:
            return
        _send_message(self._token, self._last_chat_id, message, parse_mode=None)

    def _is_allowed(self, chat_id: int) -> bool:
        return not self._allowed or chat_id in self._allowed

    @staticmethod
    def _help_text() -> str:
        return (
            "Agent Hub\n"
            "Send a plain message to dispatch it to a specialist agent "
            "(e.g. AI Tech Lead). Slash commands control the hub itself:\n\n"
            "/help - show this\n"
            "/agents - list registered specialist agents\n"
            "/new - start a fresh conversation\n"
            "/status - show the active or paused task\n"
            "/last - show the most recently finished task\n"
            "/stop - cancel the active task\n"
            "/approve - approve a task waiting on approval\n"
            "/reject [reason] - reject a task waiting on approval\n"
            "/learn <fact> - store an explicit learning\n"
            "/memory - list stored learnings\n"
            "/forget <id> - remove a stored learning\n"
            "/learn-mode [on|off] - toggle automatic background learning "
            "(off by default; shows status with no argument)\n"
            "/project [<path>|clear] - set/show/clear the target project "
            "passed to specialists (shows current with no argument)\n"
        )

    def _handle_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        text = msg.get("text", "").strip()

        if not self._is_allowed(chat_id):
            logger.info("Ignored message from unauthorized chat %d", chat_id)
            return

        self._last_chat_id = chat_id
        logger.info("Telegram message from chat %d: %s", chat_id, text)

        if text == "/help":
            _send_message(self._token, chat_id, self._help_text(), parse_mode=None)
            return

        if text == "/new":
            self._orch.new_session()
            _send_message(self._token, chat_id, "Started a fresh conversation.")
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
                reply = self._orch.approve_pending()
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

        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                _send_message(
                    self._token,
                    chat_id,
                    (
                        "A task is already running. "
                        "Use /status or /stop before sending another request."
                    ),
                    parse_mode=None,
                )
                return
            worker = threading.Thread(
                target=self._process_user_message,
                args=(chat_id, text),
                daemon=True,
            )
            self._worker = worker
        worker.start()

    def _process_user_message(self, chat_id: int, text: str) -> None:
        try:
            pending = self._orch.pending_run()
            if pending is not None and pending.state == "waiting_clarification":
                reply = self._orch.provide_clarification(text)
            else:
                reply = self._orch.invoke(text)
        except TaskCancelled:
            logger.info("Task was cancelled before completion message delivery.")
            return
        except Exception as exc:
            logger.exception("Orchestrator error")
            reply = f"Error: {exc}"

        _send_message(self._token, chat_id, reply)

    def run(self) -> None:
        logger.info("Hub Telegram gateway starting (session %s).", self._orch.session_id)
        offset = 0
        try:
            while True:
                updates = _get_updates(self._token, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message") or update.get("edited_message")
                    if msg:
                        self._handle_message(msg)
                if not updates:
                    time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Hub Telegram gateway stopped by user (Ctrl-C).")


def run_telegram(token: str | None = None) -> None:
    from dotenv import load_dotenv

    from .startup_health import ensure_healthy_startup

    load_dotenv()
    ensure_healthy_startup("telegram", telegram_token=token)
    tok = token or os.getenv("HUB_BOT_TOKEN")
    if not tok:
        raise RuntimeError("HUB_BOT_TOKEN is not set.")

    orch = HubOrchestrator()
    gateway = TelegramGateway(tok, orch)
    gateway.run()
