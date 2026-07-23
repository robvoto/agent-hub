"""Tests for CLI help text."""

from __future__ import annotations

from agent_hub.cli import _HELP_TEXT


def test_help_text_explains_current_thread_controls() -> None:
    assert "Reply normally to continue a clarification pause in the same thread." in _HELP_TEXT
    assert "Use /approve to continue an approval pause in the same thread." in _HELP_TEXT
    assert "/new starts a fresh empty thread; it is not a fork." in _HELP_TEXT
    assert "Cancelled work from /stop or /reset is not resumable." in _HELP_TEXT
    assert "There is no /fork or generic /resume command yet." in _HELP_TEXT

