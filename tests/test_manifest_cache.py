from __future__ import annotations

import json
from types import SimpleNamespace

from agent_hub.manifest_cache import (
    DEFAULT_MANIFEST_TTL_SECONDS,
    MANIFEST_CACHE_TTL_FIELD,
    ManifestCache,
)


def test_get_or_refresh_uses_declared_manifest_cache_ttl(monkeypatch, tmp_path) -> None:
    cache = ManifestCache(tmp_path / "agent_manifest_cache.json")
    spec = SimpleNamespace(
        id="ai-tech-lead",
        runtime={"working_directory": str(tmp_path)},
    )

    monkeypatch.setattr(
        "agent_hub.manifest_cache.derive_manifest_command",
        lambda _runtime: "fake-manifest",
    )

    class _Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "agent_id": "ai-tech-lead",
                "manifest_hash": "abc123",
                MANIFEST_CACHE_TTL_FIELD: 900,
            }
        )

    monkeypatch.setattr(
        "agent_hub.manifest_cache.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )

    record = cache.get_or_refresh(spec)

    assert record is not None
    assert record.ttl_seconds == 900


def test_get_or_refresh_falls_back_to_default_ttl_when_manifest_omits_it(
    monkeypatch, tmp_path
) -> None:
    cache = ManifestCache(tmp_path / "agent_manifest_cache.json")
    spec = SimpleNamespace(
        id="ai-tech-lead",
        runtime={"working_directory": str(tmp_path)},
    )

    monkeypatch.setattr(
        "agent_hub.manifest_cache.derive_manifest_command",
        lambda _runtime: "fake-manifest",
    )

    class _Result:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "agent_id": "ai-tech-lead",
                "manifest_hash": "abc123",
            }
        )

    monkeypatch.setattr(
        "agent_hub.manifest_cache.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )

    record = cache.get_or_refresh(spec)

    assert record is not None
    assert record.ttl_seconds == DEFAULT_MANIFEST_TTL_SECONDS


def test_update_reference_preserves_existing_manifest_and_ttl(tmp_path) -> None:
    cache = ManifestCache(tmp_path / "agent_manifest_cache.json")
    spec_record = {
        "ai-tech-lead": {
            "agent_id": "ai-tech-lead",
            "manifest_hash": "old-hash",
            "manifest_command": "uv run python -m ai_tech_lead manifest",
            "fetched_at": "2026-07-22T12:00:00+00:00",
            "ttl_seconds": 900,
            "manifest": {
                "agent_id": "ai-tech-lead",
                "purpose": "Existing routing purpose",
                "manifest_hash": "old-hash",
                MANIFEST_CACHE_TTL_FIELD: 900,
            },
        }
    }
    cache._cache_file.write_text(json.dumps(spec_record), encoding="utf-8")

    record = cache.update_reference(
        "ai-tech-lead",
        {
            "agent_id": "ai-tech-lead",
            "manifest_hash": "new-hash",
            "manifest_command": "uv run python -m ai_tech_lead manifest",
            "package_version": "1.2.3",
        },
    )

    assert record.ttl_seconds == 900
    assert record.manifest["purpose"] == "Existing routing purpose"
    assert record.manifest["package_version"] == "1.2.3"
    assert record.manifest["manifest_hash"] == "new-hash"


def test_description_for_falls_back_to_purpose_when_no_manifest(monkeypatch, tmp_path) -> None:
    cache = ManifestCache(tmp_path / "agent_manifest_cache.json")
    spec = SimpleNamespace(
        id="ai-tech-lead",
        name="AI Tech Lead",
        purpose="Specialist coding agent for software engineering tasks.",
        runtime={"working_directory": str(tmp_path)},
    )
    monkeypatch.setattr(
        "agent_hub.manifest_cache.derive_manifest_command",
        lambda _runtime: None,
    )

    description = cache.description_for(spec)

    assert description == "AI Tech Lead: Specialist coding agent for software engineering tasks."
