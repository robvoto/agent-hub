"""Tests for runtime model selection."""

from __future__ import annotations

import pytest

from agent_hub.config import ConfigurationError, configured_model


def test_configured_model_reads_runtime_environment(monkeypatch):
    monkeypatch.setenv("HUB_MODEL", "runtime-selected-model")

    assert configured_model() == "runtime-selected-model"


def test_configured_model_fails_closed_when_missing(monkeypatch):
    monkeypatch.delenv("HUB_MODEL", raising=False)

    with pytest.raises(ConfigurationError, match="HUB_MODEL is required"):
        configured_model()
