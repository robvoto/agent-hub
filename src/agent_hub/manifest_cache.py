"""Persistent cache of specialist agent manifests keyed by agent id and manifest hash."""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import MANIFEST_CACHE_FILE
from .runtime_policy import derive_manifest_command

logger = logging.getLogger(__name__)
DEFAULT_MANIFEST_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ManifestRecord:
    agent_id: str
    manifest_hash: str | None
    manifest_command: str | None
    fetched_at: str
    ttl_seconds: int
    manifest: dict[str, Any]

    @property
    def fetched_datetime(self) -> datetime:
        return datetime.fromisoformat(self.fetched_at)

    def is_expired(self) -> bool:
        expires_at = self.fetched_datetime + timedelta(seconds=self.ttl_seconds)
        return datetime.now(timezone.utc) >= expires_at


class ManifestCache:
    def __init__(self, cache_file: Path | None = None) -> None:
        self._cache_file = cache_file or MANIFEST_CACHE_FILE

    def get(self, agent_id: str) -> ManifestRecord | None:
        payload = self._read_cache()
        record = payload.get(agent_id)
        if not isinstance(record, dict):
            return None
        return ManifestRecord(**record)

    def get_or_refresh(self, spec: Any, *, force: bool = False) -> ManifestRecord | None:
        cached = self.get(spec.id)
        if cached is not None and not force and not cached.is_expired():
            return cached

        manifest_command = derive_manifest_command(spec.runtime)
        if not manifest_command:
            return cached

        record = self._fetch_manifest(
            spec.id,
            manifest_command,
            spec.runtime.get("working_directory"),
        )
        self._write_record(record)
        return record

    def update_reference(self, agent_id: str, reference: dict[str, Any]) -> ManifestRecord:
        manifest_hash = _string_or_none(reference.get("manifest_hash"))
        manifest_command = _string_or_none(reference.get("manifest_command"))
        manifest = {
            "agent_id": agent_id,
            "package_version": _string_or_none(reference.get("package_version")),
            "manifest_hash": manifest_hash,
        }
        record = ManifestRecord(
            agent_id=agent_id,
            manifest_hash=manifest_hash,
            manifest_command=manifest_command,
            fetched_at=_utcnow(),
            ttl_seconds=DEFAULT_MANIFEST_TTL_SECONDS,
            manifest=manifest,
        )
        self._write_record(record)
        return record

    def description_for(self, spec: Any) -> str:
        record = self.get_or_refresh(spec)
        if record is None:
            return f"{spec.name}: {spec.purpose}"
        one_line = _string_or_none(record.manifest.get("one_line"))
        if one_line:
            return f"{spec.name}: {one_line}"
        capabilities = record.manifest.get("capabilities")
        if isinstance(capabilities, list) and capabilities:
            joined = "; ".join(str(item) for item in capabilities[:2])
            return f"{spec.name}: {joined}"
        return f"{spec.name}: {spec.purpose}"

    def _fetch_manifest(
        self,
        agent_id: str,
        manifest_command: str,
        working_directory: str | None,
    ) -> ManifestRecord:
        proc = subprocess.run(
            shlex.split(manifest_command),
            cwd=working_directory or None,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Manifest command failed for '{agent_id}' (exit={proc.returncode}): "
                f"{proc.stderr.strip()}"
            )
        manifest = json.loads(proc.stdout)
        ttl_seconds = _ttl_seconds_from_manifest(manifest)
        return ManifestRecord(
            agent_id=agent_id,
            manifest_hash=_string_or_none(manifest.get("manifest_hash")),
            manifest_command=manifest_command,
            fetched_at=_utcnow(),
            ttl_seconds=ttl_seconds,
            manifest=manifest,
        )

    def _read_cache(self) -> dict[str, Any]:
        if not self._cache_file.exists():
            return {}
        data = json.loads(self._cache_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Manifest cache must be a JSON object: {self._cache_file}")
        return data

    def _write_record(self, record: ManifestRecord) -> None:
        data = self._read_cache()
        data[record.agent_id] = asdict(record)
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


_cache: ManifestCache | None = None


def get_manifest_cache(cache_file: Path | None = None) -> ManifestCache:
    global _cache
    if _cache is None:
        _cache = ManifestCache(cache_file)
        logger.info("Hub manifest cache: %s", _cache._cache_file)
    return _cache


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ttl_seconds_from_manifest(manifest: dict[str, Any]) -> int:
    army_integration = manifest.get("army_integration")
    if isinstance(army_integration, dict):
        ttl = army_integration.get("handshake_ttl_seconds")
        if isinstance(ttl, int) and ttl > 0:
            return ttl
    return DEFAULT_MANIFEST_TTL_SECONDS


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
