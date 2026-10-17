"""Opt-in bridge: OpenCode supervisor outbox -> Hub's Telegram operator channel.

What this is
------------
The OpenCode completion supervisor (a separate, read-only component) writes a
durable SQLite ``events`` outbox. This module lets the Hub **read** that outbox
and forward each new event to the operator through Hub's *existing* Telegram
gateway (the same bot, the same chat allow-list, the same ``_send_message``).

Trusted local boundary
----------------------
The integration point is a single local SQLite file, opened **read-only**
(``mode=ro``). There is no inbound HTTP port, no second bot, and no credential
duplication. Hub never writes to the outbox; Hub only reads it.

The row content is **untrusted status evidence**. It is rendered as an inert,
plain-text operator notification and nothing else. It can never approve or deny
a permission, run a command, or change Hub policy, regardless of its text —
this module only ever calls one function: the operator send callable.

Advisory only
-------------
This SQLite file is *same-host status data*, not authenticated or certified
evidence. ``DONE_REPORTED`` is the worker's own claim; ``DONE_VERIFIED`` only
means the supervisor had a separately trusted verifier record it, and Hub still
cannot independently confirm it from this channel. Both are therefore
**advisory** operator updates, never facts, approvals or decisions.

Delivery semantics
------------------
At-least-once, not exactly-once. An event id is written to Hub's own
append-only ledger only *after* the operator send succeeds, so a failed or
crashed send is retried on the next poll. Duplicates are suppressed by the
ledger, keyed on the outbox event identity (``events.dedupe_key``).

Bounded page consumption
------------------------
Events are read oldest-first, one batch per poll, using a durable cursor that
only advances past events that were delivered or deliberately filtered out. A
send failure stops the cursor so the event is retried, and a backlog larger than
one batch raises a visible warning instead of silently hiding older events.
The ledger and cursor are tolerant of partial writes, malformed rows and
process restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import DATA_DIR, ConfigurationError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# configuration (opt-in; disabled unless explicitly enabled)
# --------------------------------------------------------------------------- #
ENABLED_ENV = "HUB_OPENCODE_NOTIFY_ENABLED"
OUTBOX_ENV = "HUB_OPENCODE_OUTBOX_DB"
LEDGER_ENV = "HUB_OPENCODE_NOTIFY_LEDGER"
STATES_ENV = "HUB_OPENCODE_NOTIFY_STATES"
MAX_EVENTS_ENV = "HUB_OPENCODE_NOTIFY_MAX_EVENTS"

DEFAULT_LEDGER_PATH = DATA_DIR / "opencode_notify_ledger.jsonl"
DEFAULT_MAX_EVENTS = 50
MAX_MAX_EVENTS = 500
MAX_TEXT_CHARS = 1800

#: Supervisor states this bridge understands. Unknown values are ignored.
KNOWN_STATES = frozenset(
    {
        "PENDING",
        "RUNNING",
        "STALLED",
        "BLOCKED_PERMISSION",
        "DONE_REPORTED",
        "DONE_VERIFIED",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
    }
)

#: Default notification filter. RUNNING/PENDING are progress noise, not events
#: an operator needs pushed; the rest are actionable outcomes.
DEFAULT_NOTIFY_STATES = frozenset(KNOWN_STATES - {"RUNNING", "PENDING"})

#: Human labels. Kept explicit so a new state cannot silently render as its id.
STATE_LABELS = {
    "STALLED": "stalled (no progress)",
    "BLOCKED_PERMISSION": "permission required",
    "DONE_REPORTED": "reported complete (worker self-report — needs verification)",
    "DONE_VERIFIED": "reported verified (advisory)",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "UNKNOWN": "unknown (needs review)",
}

#: Evidence keys that are safe to echo to the operator. Everything else in the
#: (untrusted) evidence object is ignored, so an unexpected key cannot leak.
SAFE_EVIDENCE_KEYS = ("reported_tests", "reported_commit", "verification")

_ADVISORY_FOOTER = (
    "advisory status from the local OpenCode supervisor outbox; "
    "Hub cannot independently verify it and it is not an approval."
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Cursor: an exclusive ``(created_at, dedupe_key)`` lower bound.
Cursor = tuple[float, str]


# --------------------------------------------------------------------------- #
# event model + reader
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    task_id: str
    state: str
    severity: str
    created_at: float
    session_id: str | None = None
    repo: str | None = None
    worktree: str | None = None
    reason: str | None = None
    previous_state: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


_SELECT = (
    "SELECT dedupe_key, task_id, state, severity, created_at, payload_json FROM events "
)


def read_outbox(
    db_path: str | os.PathLike[str],
    *,
    limit: int = DEFAULT_MAX_EVENTS,
    after: Cursor | None = None,
) -> tuple[list[OutboxEvent], bool]:
    """Read one oldest-first batch of events, read-only.

    Returns ``(events, has_more)``. ``after`` is an exclusive
    ``(created_at, dedupe_key)`` cursor; ``None`` starts from the oldest event.
    ``has_more`` is ``True`` when more rows exist beyond this batch, so callers
    can warn about a backlog rather than silently hiding older events.

    Malformed rows are skipped (and logged by identity only), never raised.
    """
    path = Path(db_path)
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        if after is None:
            rows = connection.execute(
                _SELECT + "ORDER BY created_at ASC, dedupe_key ASC LIMIT ?",
                (int(limit) + 1,),
            ).fetchall()
        else:
            timestamp, event_id = after
            rows = connection.execute(
                _SELECT
                + "WHERE (created_at > ?) OR (created_at = ? AND dedupe_key > ?) "
                + "ORDER BY created_at ASC, dedupe_key ASC LIMIT ?",
                (float(timestamp), float(timestamp), str(event_id), int(limit) + 1),
            ).fetchall()
    finally:
        connection.close()

    has_more = len(rows) > limit
    events = [
        event
        for event in (_row_to_event(row) for row in rows[:limit])
        if event is not None
    ]
    return events, has_more


def _row_to_event(row: sqlite3.Row) -> OutboxEvent | None:
    event_id = row["dedupe_key"]
    raw = row["payload_json"]
    try:
        payload = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        logger.warning("OpenCode outbox row %s has malformed payload; skipping.", _short(event_id))
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "OpenCode outbox row %s payload is not an object; skipping.", _short(event_id)
        )
        return None

    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}

    return OutboxEvent(
        event_id=str(event_id),
        task_id=_str(payload.get("task_id"), row["task_id"]),
        state=_str(row["state"], payload.get("state")),
        severity=_str(row["severity"], payload.get("severity")),
        created_at=_float(row["created_at"], payload.get("created_at")),
        session_id=_opt_str(payload.get("session_id")),
        repo=_opt_str(payload.get("repo")),
        worktree=_opt_str(payload.get("worktree")),
        reason=_opt_str(payload.get("reason")),
        previous_state=_opt_str(payload.get("previous_state")),
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# rendering (inert plain text; no commands, no approvals)
# --------------------------------------------------------------------------- #
def render_notification(event: OutboxEvent) -> str:
    label = STATE_LABELS.get(event.state, event.state)
    lines = [f"[OpenCode] {label}", f"task: {_clean(event.task_id, 120)}"]
    if event.repo:
        lines.append(f"repo: {_clean(event.repo, 120)}")
    if event.worktree:
        lines.append(f"worktree: {_clean(event.worktree, 200)}")
    if event.reason:
        lines.append(f"detail: {_clean(event.reason, 300)}")

    if event.state == "DONE_REPORTED":
        bits = [
            f"{key}={_clean(str(event.evidence[key]), 60)}"
            for key in SAFE_EVIDENCE_KEYS
            if event.evidence.get(key)
        ]
        if bits:
            lines.append("worker-reported: " + " ".join(bits))
        lines.append(
            "note: worker self-report only; not independently verified. "
            "Confirm files, commit and tests before treating the task as done."
        )
    elif event.state == "BLOCKED_PERMISSION":
        lines.append(
            "action: a worker is waiting for a permission decision. Approve or deny it "
            "in the OpenCode UI; this Hub notification cannot approve it."
        )

    lines.append(_ADVISORY_FOOTER)
    return "\n".join(lines)[:MAX_TEXT_CHARS]


def _clean(value: str, limit: int) -> str:
    text = _CONTROL_RE.sub(" ", value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(1, limit - 1)] + "…"
    return text


# --------------------------------------------------------------------------- #
# ledger + cursor (idempotent consumer, restart-safe)
# --------------------------------------------------------------------------- #
class ConsumedLedger:
    """Append-only set of delivered event ids.

    A truncated final line (partial write) is ignored on load, so its event may
    be re-delivered — that is the documented at-least-once behaviour.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._seen: set[str] = set()
        for item in self._read_items():
            event_id = item.get("event_id")
            if isinstance(event_id, str) and event_id:
                self._seen.add(event_id)

    def _read_items(self) -> Iterable[dict]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue  # partial/corrupt line: ignore, allow re-delivery
                if isinstance(item, dict):
                    yield item

    def has(self, event_id: str) -> bool:
        return event_id in self._seen

    def record(self, event_id: str, *, at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"event_id": event_id, "at": at}, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        self._seen.add(event_id)

    def __len__(self) -> int:
        return len(self._seen)


class ProgressCursor:
    """Durable oldest-unconsumed cursor written atomically.

    A missing or corrupt file degrades to ``None`` (re-read from the oldest
    event); the ledger then suppresses any re-delivery, so this is safe.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> Cursor | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        timestamp = data.get("created_at")
        event_id = data.get("dedupe_key")
        if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
            return None
        if not isinstance(event_id, str) or not event_id:
            return None
        return (float(timestamp), event_id)

    def save(self, cursor: Cursor) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"created_at": cursor[0], "dedupe_key": cursor[1]}, sort_keys=True
        )
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


# --------------------------------------------------------------------------- #
# bridge
# --------------------------------------------------------------------------- #
class OpenCodeNotificationBridge:
    """Polls the OpenCode outbox and forwards new events to the operator."""

    def __init__(
        self,
        *,
        outbox_db: str | os.PathLike[str],
        send: Callable[[str], bool],
        ledger_path: str | os.PathLike[str] = DEFAULT_LEDGER_PATH,
        max_events: int = DEFAULT_MAX_EVENTS,
        states: Iterable[str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._outbox_db = Path(outbox_db)
        self._send = send
        self._ledger = ConsumedLedger(ledger_path)
        self._cursor = ProgressCursor(str(ledger_path) + ".cursor.json")
        self._max_events = max(1, min(int(max_events), MAX_MAX_EVENTS))
        self._states = frozenset(states) if states is not None else DEFAULT_NOTIFY_STATES
        self._clock = clock
        self.last_error: str | None = None

    @property
    def ledger(self) -> ConsumedLedger:
        return self._ledger

    @property
    def cursor(self) -> ProgressCursor:
        return self._cursor

    def poll_once(self) -> int:
        """Deliver one bounded batch of not-yet-delivered events.

        Returns the number of notifications actually sent this poll. The cursor
        only advances past events that were delivered or deliberately filtered;
        a send failure stops it so the event is retried on the next poll.
        """
        try:
            events, has_more = read_outbox(
                self._outbox_db, limit=self._max_events, after=self._cursor.load()
            )
        except (sqlite3.Error, OSError) as exc:
            self.last_error = type(exc).__name__
            logger.warning(
                "OpenCode outbox unreadable (%s); skipping this poll.", type(exc).__name__
            )
            return 0
        self.last_error = None
        if has_more:
            logger.warning(
                "OpenCode outbox backlog exceeds one batch (%d events); remaining events "
                "will be delivered on later polls.",
                self._max_events,
            )

        delivered = 0
        last_examined: OutboxEvent | None = None
        for event in events:
            if event.state in self._states and not self._ledger.has(event.event_id):
                title = _short(event.event_id)
                try:
                    ok = bool(self._send(render_notification(event)))
                except Exception as exc:  # noqa: BLE001 - retry next poll, never crash the bot
                    logger.warning(
                        "OpenCode notification send failed for %s (%s); will retry.",
                        title,
                        type(exc).__name__,
                    )
                    break
                if not ok:
                    logger.warning(
                        "OpenCode notification not confirmed for %s; will retry.", title
                    )
                    break
                self._ledger.record(event.event_id, at=self._clock())
                delivered += 1
            last_examined = event

        if last_examined is not None:
            self._cursor.save((last_examined.created_at, last_examined.event_id))
        return delivered


# --------------------------------------------------------------------------- #
# environment wiring
# --------------------------------------------------------------------------- #
def notifications_enabled() -> bool:
    return os.getenv(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def build_bridge_from_env(send: Callable[[str], bool]) -> OpenCodeNotificationBridge | None:
    """Build the bridge only when explicitly enabled; otherwise return ``None``."""
    if not notifications_enabled():
        return None

    outbox = os.getenv(OUTBOX_ENV, "").strip()
    if not outbox:
        raise ConfigurationError(
            f"{ENABLED_ENV} is enabled but {OUTBOX_ENV} (path to the OpenCode supervisor "
            "outbox database) is not set. Refusing to start without a source."
        )

    ledger_path = os.getenv(LEDGER_ENV, "").strip() or str(DEFAULT_LEDGER_PATH)
    max_events = _env_int(MAX_EVENTS_ENV, DEFAULT_MAX_EVENTS)
    states = _env_states(STATES_ENV)
    return OpenCodeNotificationBridge(
        outbox_db=outbox,
        send=send,
        ledger_path=ledger_path,
        max_events=max_events,
        states=states,
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if value < 1 or value > MAX_MAX_EVENTS:
        raise ConfigurationError(f"{name} must be between 1 and {MAX_MAX_EVENTS}.")
    return value


def _env_states(name: str) -> frozenset[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return DEFAULT_NOTIFY_STATES
    states = {part.strip().upper() for part in raw.split(",") if part.strip()}
    unknown = states - KNOWN_STATES
    if unknown:
        raise ConfigurationError(
            f"{name} contains unknown state(s): {', '.join(sorted(unknown))}."
        )
    if not states:
        raise ConfigurationError(f"{name} must list at least one state.")
    return frozenset(states)


def _short(value: Any) -> str:
    text = str(value)
    return text[:12]


def _str(primary: Any, fallback: Any) -> str:
    for candidate in (primary, fallback):
        if isinstance(candidate, str) and candidate:
            return candidate
    return "?"


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _float(primary: Any, fallback: Any) -> float:
    for candidate in (primary, fallback):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return float(candidate)
    return 0.0
