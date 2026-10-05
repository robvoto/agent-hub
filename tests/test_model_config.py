"""Tests for runtime model selection."""

from __future__ import annotations

import pytest

from agent_hub.config import (
    ConfigurationError,
    chat_model_kwargs,
    configured_model,
    configured_reasoning_effort,
    handoff_reviewer_model_kwargs,
)


def test_configured_model_reads_runtime_environment(monkeypatch):
    monkeypatch.setenv("HUB_MODEL", "runtime-selected-model")

    assert configured_model() == "runtime-selected-model"


def test_configured_model_fails_closed_when_missing(monkeypatch):
    monkeypatch.delenv("HUB_MODEL", raising=False)

    with pytest.raises(ConfigurationError, match="HUB_MODEL is required"):
        configured_model()


def test_reasoning_effort_is_optional_and_provider_neutral(monkeypatch):
    monkeypatch.delenv("HUB_REASONING_EFFORT", raising=False)
    assert configured_reasoning_effort() is None
    assert chat_model_kwargs("any-model") == {"model": "any-model"}

    monkeypatch.setenv("HUB_REASONING_EFFORT", "none")
    assert chat_model_kwargs("any-model") == {
        "model": "any-model",
        "reasoning_effort": "none",
    }


def test_reasoning_effort_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("HUB_REASONING_EFFORT", "turbo")
    with pytest.raises(ConfigurationError, match="HUB_REASONING_EFFORT"):
        configured_reasoning_effort()


def test_handoff_reviewer_uses_explicit_reasoning_effort(monkeypatch):
    monkeypatch.setenv("HUB_REASONING_EFFORT", "high")
    monkeypatch.delenv("HUB_HANDOFF_REVIEW_REASONING_EFFORT", raising=False)

    assert handoff_reviewer_model_kwargs("review-model") == {
        "model": "review-model",
        "reasoning_effort": "none",
    }

    monkeypatch.setenv("HUB_HANDOFF_REVIEW_REASONING_EFFORT", "low")
    assert handoff_reviewer_model_kwargs("review-model") == {
        "model": "review-model",
        "reasoning_effort": "low",
    }
