"""Validation and tailing for specialist progress event streams."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .task_runs import (
    PROGRESS_MODE_PENDING,
    PROGRESS_MODE_UNAVAILABLE,
    get_task_run_store,
)

PROGRESS_SCHEMA_VERSION = 1
PROGRESS_POLL_INTERVAL_SECONDS = float(os.getenv("HUB_PROGRESS_POLL_INTERVAL_SECONDS", "0.20"))
PROGRESS_HEARTBEAT_INTERVAL_SECONDS = float(
    os.getenv("HUB_PROGRESS_HEARTBEAT_INTERVAL_SECONDS", "30.0")
)
PROGRESS_STALE_AFTER_SECONDS = float(os.getenv("HUB_PROGRESS_STALE_AFTER_SECONDS", "90.0"))

_MAX_LINE_BYTES = int(os.getenv("HUB_PROGRESS_MAX_LINE_BYTES", "8192"))
_MAX_SUMMARY_CHARS = 280
_MAX_PHASE_CHARS = 64
_MAX_METADATA_KEYS = 12
_MAX_METADATA_STRING_CHARS = 240
_MAX_METADATA_JSON_BYTES = 1024
_EVENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_PHASE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/ -]{0,63}$")
_BANNED_METADATA_KEYS = {
    "api_key",
    "chain_of_thought",
    "cot",
    "full_prompt",
    "log",
    "logs",
    "prompt",
    "provider_payload",
    "raw_payload",
    "raw_request",
    "raw_response",
    "reasoning",
    "secret",
    "secrets",
    "token",
}
_NOTIFY_EVENT_TYPES = {
    "failure",
    "heartbeat",
    "info",
    "phase",
    "start",
    "waiting",
    "warning",
}


@dataclass(frozen=True)
class ProgressUpdate:
    run_id: str
    event_type: str
    phase: str | None
    human_summary: str
    occurred_at: datetime
    sequence: int | None = None


@dataclass(frozen=True)
class ValidatedProgressEvent:
    schema_version: int
    run_id: str
    request_id: str
    sequence: int
    event_type: str
    phase: str
    human_summary: str
    occurred_at: datetime
    metadata: dict[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    if not collapsed or len(collapsed) > limit:
        return None
    return collapsed


def _normalize_phase(value: Any) -> str | None:
    phase = _normalize_text(value, limit=_MAX_PHASE_CHARS)
    if phase is None or _PHASE_RE.fullmatch(phase) is None:
        return None
    return phase


def _normalize_event_type(value: Any) -> str | None:
    event_type = _normalize_text(value, limit=32)
    if event_type is None or _EVENT_NAME_RE.fullmatch(event_type) is None:
        return None
    return event_type


def _normalize_metadata(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "metadata must be an object"
    if len(value) > _MAX_METADATA_KEYS:
        return None, "metadata has too many keys"

    normalized: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            return None, "metadata keys must be non-empty strings"
        if key.lower() in _BANNED_METADATA_KEYS:
            return None, f"metadata key '{key}' is not allowed"
        if isinstance(item, (bool, int, float)) or item is None:
            normalized[key] = item
            continue
        if isinstance(item, str):
            collapsed = " ".join(item.split())
            if len(collapsed) > _MAX_METADATA_STRING_CHARS:
                return None, f"metadata value for '{key}' is too long"
            normalized[key] = collapsed
            continue
        return None, f"metadata value for '{key}' must be scalar"

    raw = json.dumps(normalized, sort_keys=True)
    if len(raw.encode("utf-8")) > _MAX_METADATA_JSON_BYTES:
        return None, "metadata payload is too large"
    return normalized, None


def validate_specialist_progress_event(
    payload: Any,
    *,
    expected_run_id: str,
    expected_request_id: str,
    last_sequence: int,
) -> tuple[ValidatedProgressEvent | None, str | None]:
    if not isinstance(payload, dict):
        return None, "event must be a JSON object"

    schema_version = payload.get("schema_version")
    if schema_version != PROGRESS_SCHEMA_VERSION:
        return None, f"unsupported schema_version '{schema_version}'"

    run_id = _normalize_text(payload.get("run_id"), limit=80)
    if run_id is None:
        return None, "run_id is required"
    if run_id != expected_run_id:
        return None, "run_id does not match the current task run"

    request_id = _normalize_text(payload.get("request_id"), limit=80)
    if request_id is None:
        return None, "request_id is required"
    if request_id != expected_request_id:
        return None, "request_id does not match the current specialist request"

    sequence = payload.get("sequence")
    if not isinstance(sequence, int) or sequence < 1:
        return None, "sequence must be a positive integer"
    if sequence <= last_sequence:
        if sequence == last_sequence:
            return None, "duplicate sequence"
        return None, "out-of-order sequence"

    event_type = _normalize_event_type(payload.get("event_type"))
    if event_type is None:
        return None, "event_type is invalid"

    phase = _normalize_phase(payload.get("phase"))
    if phase is None:
        return None, "phase is invalid"

    human_summary = _normalize_text(payload.get("human_summary"), limit=_MAX_SUMMARY_CHARS)
    if human_summary is None:
        return None, "human_summary is invalid"

    occurred_at = _parse_timestamp(payload.get("occurred_at"))
    if occurred_at is None:
        return None, "occurred_at must be an ISO-8601 timestamp"

    metadata, metadata_error = _normalize_metadata(payload.get("metadata"))
    if metadata is None:
        return None, metadata_error or "metadata is invalid"

    return (
        ValidatedProgressEvent(
            schema_version=schema_version,
            run_id=run_id,
            request_id=request_id,
            sequence=sequence,
            event_type=event_type,
            phase=phase,
            human_summary=human_summary,
            occurred_at=occurred_at,
            metadata=metadata,
        ),
        None,
    )


def _rejected_line_fingerprint(raw_line: str) -> str:
    return hashlib.sha256(raw_line.encode("utf-8", errors="replace")).hexdigest()[:16]


class SpecialistProgressTailer:
    """Incrementally read, validate, and persist a subprocess progress JSONL stream."""

    def __init__(
        self,
        *,
        run_id: str,
        request_id: str,
        path: Path,
        specialist_name: str,
    ) -> None:
        self._run_id = run_id
        self._request_id = request_id
        self._path = path
        self._specialist_name = specialist_name
        self._offset = 0
        self._buffer = ""
        self._last_sequence = 0
        self._last_stream_activity_monotonic: float | None = None
        self._last_heartbeat_monotonic: float | None = None
        self._last_phase = "starting"
        self._saw_specialist_event = False

    def begin(self) -> list[ProgressUpdate]:
        store = get_task_run_store()
        store.set_progress_mode(self._run_id, PROGRESS_MODE_PENDING)
        now = _utcnow()
        store.record_progress_event(
            self._run_id,
            schema_version=PROGRESS_SCHEMA_VERSION,
            event_run_id=self._run_id,
            request_id=self._request_id,
            sequence=None,
            event_type="start",
            phase="starting",
            human_summary=f"{self._specialist_name} started.",
            occurred_at=now,
            metadata={"source": "hub"},
            validation_status="accepted",
            validation_message="Hub dispatch start acknowledgement.",
            raw_json=None,
            promote_mode=False,
        )
        return [
            ProgressUpdate(
                run_id=self._run_id,
                event_type="start",
                phase="starting",
                human_summary=f"{self._specialist_name} started.",
                occurred_at=now,
            )
        ]

    def poll(self, *, final: bool = False) -> list[ProgressUpdate]:
        updates: list[ProgressUpdate] = []
        if self._path.exists():
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
            if chunk:
                self._buffer += chunk
                lines = self._buffer.splitlines(keepends=True)
                self._buffer = ""
                for line in lines:
                    if line.endswith("\n"):
                        update = self._process_line(line.rstrip("\r\n"))
                        if update is not None:
                            updates.append(update)
                    else:
                        self._buffer = line
        if final and self._buffer.strip():
            update = self._process_line(self._buffer, partial_ok=True)
            if update is not None:
                updates.append(update)
            self._buffer = ""
        return updates

    def process_line(self, raw_line: str) -> ProgressUpdate | None:
        """Validate and persist one JSONL line from a live stdout stream."""

        return self._process_line(raw_line.rstrip("\r\n"))

    def maybe_emit_background_update(self) -> ProgressUpdate | None:
        now = time.monotonic()
        store = get_task_run_store()

        if not self._saw_specialist_event:
            return None

        if self._last_stream_activity_monotonic is None:
            return None
        if now - self._last_stream_activity_monotonic < PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
            return None
        if self._last_heartbeat_monotonic is not None and (
            now - self._last_heartbeat_monotonic < PROGRESS_HEARTBEAT_INTERVAL_SECONDS
        ):
            return None

        occurred_at = _utcnow()
        self._last_heartbeat_monotonic = now
        store.record_progress_event(
            self._run_id,
            schema_version=PROGRESS_SCHEMA_VERSION,
            event_run_id=self._run_id,
            request_id=self._request_id,
            sequence=None,
            event_type="heartbeat",
            phase=self._last_phase,
            human_summary=f"Still working: {self._last_phase}.",
            occurred_at=occurred_at,
            metadata={"source": "hub"},
            validation_status="accepted",
            validation_message="Hub heartbeat after quiet interval.",
            raw_json=None,
            promote_mode=False,
        )
        return ProgressUpdate(
            run_id=self._run_id,
            event_type="heartbeat",
            phase=self._last_phase,
            human_summary=f"Still working: {self._last_phase}.",
            occurred_at=occurred_at,
        )

    def finish(self) -> None:
        return

    def mark_unavailable_if_silent(self) -> None:
        if self._saw_specialist_event:
            return
        get_task_run_store().set_progress_mode(self._run_id, PROGRESS_MODE_UNAVAILABLE)

    def _process_line(self, raw_line: str, *, partial_ok: bool = False) -> ProgressUpdate | None:
        store = get_task_run_store()
        encoded = raw_line.encode("utf-8", errors="replace")
        if len(encoded) > _MAX_LINE_BYTES:
            store.record_progress_event(
                self._run_id,
                schema_version=None,
                event_run_id=None,
                request_id=None,
                sequence=None,
                event_type=None,
                phase=None,
                human_summary=None,
                occurred_at=None,
                metadata={"rejection_fingerprint": _rejected_line_fingerprint(raw_line)},
                validation_status="rejected",
                validation_message="oversized progress line",
                raw_json=None,
            )
            return None

        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            store.record_progress_event(
                self._run_id,
                schema_version=None,
                event_run_id=None,
                request_id=None,
                sequence=None,
                event_type=None,
                phase=None,
                human_summary=None,
                occurred_at=None,
                metadata={"rejection_fingerprint": _rejected_line_fingerprint(raw_line)},
                validation_status="malformed",
                validation_message="partial progress line" if partial_ok else "invalid JSON",
                raw_json=None,
            )
            return None

        event, error = validate_specialist_progress_event(
            payload,
            expected_run_id=self._run_id,
            expected_request_id=self._request_id,
            last_sequence=self._last_sequence,
        )
        if event is None:
            store.record_progress_event(
                self._run_id,
                schema_version=payload.get("schema_version") if isinstance(payload, dict) else None,
                event_run_id=payload.get("run_id") if isinstance(payload, dict) else None,
                request_id=payload.get("request_id") if isinstance(payload, dict) else None,
                sequence=payload.get("sequence") if isinstance(payload, dict) else None,
                event_type=payload.get("event_type") if isinstance(payload, dict) else None,
                phase=payload.get("phase") if isinstance(payload, dict) else None,
                human_summary=payload.get("human_summary") if isinstance(payload, dict) else None,
                occurred_at=_parse_timestamp(payload.get("occurred_at"))
                if isinstance(payload, dict)
                else None,
                metadata={"rejection_fingerprint": _rejected_line_fingerprint(raw_line)},
                validation_status="rejected",
                validation_message=error or "invalid progress event",
                raw_json=None,
            )
            return None

        self._last_sequence = event.sequence
        self._last_phase = event.phase
        self._last_stream_activity_monotonic = time.monotonic()
        self._saw_specialist_event = True
        store.record_progress_event(
            self._run_id,
            schema_version=event.schema_version,
            event_run_id=event.run_id,
            request_id=event.request_id,
            sequence=event.sequence,
            event_type=event.event_type,
            phase=event.phase,
            human_summary=event.human_summary,
            occurred_at=event.occurred_at,
            metadata=event.metadata,
            validation_status="accepted",
            validation_message="accepted",
            raw_json=raw_line,
            promote_mode=True,
        )
        if event.event_type not in _NOTIFY_EVENT_TYPES:
            return None
        return ProgressUpdate(
            run_id=self._run_id,
            event_type=event.event_type,
            phase=event.phase,
            human_summary=event.human_summary,
            occurred_at=event.occurred_at,
            sequence=event.sequence,
        )
