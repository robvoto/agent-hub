"""Tests for the bounded read-only Hub context service (AGENT-HUB-045)."""

from __future__ import annotations

from agent_hub.hub_context import HubContextService


def _write_doc(project_root, relative_path: str, content: str) -> None:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_list_documentation_sources_is_the_fixed_approved_set(tmp_path):
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    sources = service.list_documentation_sources()

    assert sources == (
        "AGENTS.md",
        "docs/INDEX.md",
        "docs/ARCHITECTURE.md",
        "docs/COMMANDS.md",
        "docs/VALIDATION.md",
    )


def test_read_documentation_returns_content_and_freshness(tmp_path):
    _write_doc(tmp_path, "AGENTS.md", "# Agents\nRouting rules live here.")
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    result = service.read_documentation("AGENTS.md")

    assert result.available is True
    assert result.source is not None
    assert result.source.identifier == "AGENTS.md"
    assert result.source.kind == "documentation"
    assert "Routing rules live here." in result.source.content
    assert result.source.freshness  # non-empty ISO timestamp


def test_read_documentation_rejects_unapproved_identifier(tmp_path):
    _write_doc(tmp_path, "SECRETS.md", "top secret")
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    result = service.read_documentation("SECRETS.md")

    assert result.available is False
    assert result.source is None
    assert "not an approved" in result.reason


def test_read_documentation_stops_clearly_when_file_missing(tmp_path):
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    result = service.read_documentation("docs/ARCHITECTURE.md")

    assert result.available is False
    assert result.source is None
    assert "missing on disk" in result.reason


def test_read_documentation_truncates_oversized_files(tmp_path):
    _write_doc(tmp_path, "AGENTS.md", "A" * 25_000)
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    result = service.read_documentation("AGENTS.md")

    assert result.available is True
    assert len(result.source.content) < 25_000
    assert result.source.content.endswith("(truncated)")


def test_find_relevant_documentation_returns_bounded_snippets(tmp_path):
    _write_doc(
        tmp_path,
        "AGENTS.md",
        "Routing rules. " * 50 + "widget forge details here." + " padding" * 50,
    )
    _write_doc(tmp_path, "docs/INDEX.md", "Unrelated content about something else entirely.")
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    results = service.find_relevant_documentation("widget forge", max_items=3)

    assert len(results) == 1
    assert results[0].identifier == "AGENTS.md"
    assert "widget forge" in results[0].content
    assert len(results[0].content) < 1000


def test_find_relevant_documentation_respects_max_items(tmp_path):
    for doc in ("AGENTS.md", "docs/INDEX.md", "docs/ARCHITECTURE.md"):
        _write_doc(tmp_path, doc, "widget forge widget forge widget forge")
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    results = service.find_relevant_documentation("widget", max_items=2)

    assert len(results) == 2


def test_find_relevant_documentation_with_no_matches_returns_empty(tmp_path):
    _write_doc(tmp_path, "AGENTS.md", "Nothing relevant in here.")
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "agents")

    assert service.find_relevant_documentation("spaceships and rockets") == []


def test_list_manifests_reflects_live_registry(sample_registry_dir, tmp_path):
    service = HubContextService(project_root=tmp_path, registry_dir=sample_registry_dir)

    manifests = service.list_manifests()

    identifiers = {m.identifier for m in manifests}
    assert identifiers == {"manifest:code-reviewer", "manifest:job-hunter"}
    assert all(m.kind == "manifest" for m in manifests)
    reviewer = next(m for m in manifests if m.identifier == "manifest:code-reviewer")
    assert "Code Reviewer" in reviewer.content
    assert "fingerprint=" in reviewer.content


def test_get_manifest_returns_lookup_result_for_known_and_unknown_agent(
    sample_registry_dir, tmp_path
):
    service = HubContextService(project_root=tmp_path, registry_dir=sample_registry_dir)

    known = service.get_manifest("code-reviewer")
    unknown = service.get_manifest("does-not-exist")

    assert known.available is True
    assert known.source.identifier == "manifest:code-reviewer"
    assert unknown.available is False
    assert "does-not-exist" in unknown.reason


def test_runtime_metadata_reports_registry_health(sample_registry_dir, tmp_path):
    service = HubContextService(project_root=tmp_path, registry_dir=sample_registry_dir)

    metadata = service.runtime_metadata()

    assert metadata.identifier == "runtime:registry-health"
    assert metadata.kind == "runtime_metadata"
    assert "loaded_agents=2" in metadata.content
    assert "invalid_manifests=0" in metadata.content
    assert metadata.freshness


def test_runtime_metadata_reports_registry_dir_missing(tmp_path):
    service = HubContextService(project_root=tmp_path, registry_dir=tmp_path / "no-such-dir")

    metadata = service.runtime_metadata()

    assert "loaded_agents=0" in metadata.content
