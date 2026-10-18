"""Tests for the Human MCP shell-approval bridge (owner-facing Telegram view).

These tests do **not** require the approval module to be installed. When it is
absent the bridge must report itself disabled rather than fail or, worse, act as
if a decision had been made. When it *is* present, the bridge must surface the
announced queue and capture the owner's explicit decision without ever holding
signing authority itself.
"""

from __future__ import annotations

import pytest

import agent_hub.human_mcp_shell_approvals as bridge


@pytest.fixture(autouse=True)
def _owner_env(monkeypatch):
    """The owner principal is required configuration, not a default.

    Individual tests that exercise the fail-closed paths delete it explicitly.
    """
    monkeypatch.setenv("HUMAN_MCP_APPROVAL_OWNER", "owner-1")


class TestBridgeDisabled:
    def test_disabled_without_approval_dir(self, monkeypatch):
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_DIR", raising=False)
        reason = bridge.bridge_disabled_reason()
        assert reason is not None
        assert "HUMAN_MCP_APPROVAL_DIR" in reason

    def test_disabled_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path / "nonexistent"))
        reason = bridge.bridge_disabled_reason()
        assert reason is not None
        assert "does not exist" in reason

    def test_enabled_when_dir_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        assert bridge.bridge_disabled_reason() is None

    def test_disabled_when_owner_is_unset(self, monkeypatch, tmp_path):
        # A decision must name the real owner; an unset owner never falls back to a
        # guessed identity.
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_OWNER", raising=False)
        reason = bridge.bridge_disabled_reason()
        assert reason is not None
        assert "HUMAN_MCP_APPROVAL_OWNER" in reason
        assert "guessed owner" in reason

    def test_deciding_is_refused_without_an_owner(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_OWNER", raising=False)
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")
        reply = bridge.decide_shell_request(123, "abc", "approve")
        assert "unavailable" in reply


class TestRenderPending:
    def test_disabled_reports_reason(self, monkeypatch):
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_DIR", raising=False)
        assert "unavailable" in bridge.render_pending_shell_requests(123)

    def test_unauthorised_chat_cannot_read_summaries(self, monkeypatch, tmp_path):
        # HUB_ALLOWED_CHAT_IDS may be empty (any chat can talk to the bot), so a chat
        # that cannot decide must not be able to read pending host commands either.
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "999")
        reply = bridge.render_pending_shell_requests(123)
        assert "not permitted" in reply

    def test_authorised_chat_sees_the_queue(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        reply = bridge.render_pending_shell_requests(123)
        assert "abc" in reply
        assert "run pytest" in reply
        assert "/shell-approve <request-id>" in reply


class TestAllowedDecisionChats:
    def test_empty_by_default(self, monkeypatch):
        monkeypatch.delenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", raising=False)
        assert bridge.allowed_decision_chat_ids() == set()

    def test_parses_comma_separated_ids(self, monkeypatch):
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123, 456")
        assert bridge.allowed_decision_chat_ids() == {123, 456}

    def test_ignores_invalid_entries(self, monkeypatch):
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123,not-an-int,")
        assert bridge.allowed_decision_chat_ids() == {123}


class TestListPending:
    def test_returns_empty_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_DIR", raising=False)
        assert bridge.list_pending_shell_requests() == []

    def test_surfaces_announced_requests(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        pending = bridge.list_pending_shell_requests()
        assert len(pending) == 1
        assert pending[0].request_id == "abc"
        assert pending[0].summary == "run pytest"
        assert pending[0].operation_digest == "deadbeef"

    def test_excludes_already_decided_requests(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))

        class FakeRequestView:
            request_id = "abc"
            status = "approved"

        class FakeStore:
            def list_requests(self):
                return [FakeRequestView()]

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        assert bridge.list_pending_shell_requests() == []


class TestDecideShellRequest:
    def test_rejects_when_disabled(self, monkeypatch):
        monkeypatch.delenv("HUMAN_MCP_APPROVAL_DIR", raising=False)
        reply = bridge.decide_shell_request(123, "abc", "approve")
        assert "unavailable" in reply

    def test_rejects_unauthorised_chat(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "999")
        reply = bridge.decide_shell_request(123, "abc", "approve")
        assert "not permitted" in reply

    def test_rejects_unknown_request(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return []

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        reply = bridge.decide_shell_request(123, "unknown", "approve")
        assert "No pending shell request" in reply

    def test_writes_signed_decision_with_digest(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")

        written: list[dict] = []

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        def fake_channel(*args, **kwargs):
            class _C:
                def write_decision(self, **kw):
                    written.append(kw)

            return _C()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        monkeypatch.setattr(bridge, "_load_channel", fake_channel)

        reply = bridge.decide_shell_request(123, "abc", "approve")
        assert "approved" in reply
        assert len(written) == 1
        assert written[0]["request_id"] == "abc"
        assert written[0]["decision"] == "approve"
        # The decision names the exact operation it approved.
        assert written[0]["operation_digest"] == "deadbeef"
        assert written[0]["kind"] == "exact"

    def test_denial_is_recorded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")

        written: list[dict] = []

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        def fake_channel(*args, **kwargs):
            class _C:
                def write_decision(self, **kw):
                    written.append(kw)

            return _C()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        monkeypatch.setattr(bridge, "_load_channel", fake_channel)

        reply = bridge.decide_shell_request(123, "abc", "deny", "not now")
        assert "denied" in reply
        assert written[0]["decision"] == "deny"
        assert written[0]["reason"] == "not now"

    def test_signing_failure_never_approves(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HUMAN_MCP_APPROVAL_DIR", str(tmp_path))
        monkeypatch.setenv("HUB_APPROVAL_COMMAND_ALLOWED_CHAT_IDS", "123")

        class FakeStore:
            def list_requests(self):
                return []

        class FakeService:
            store = FakeStore()

        class FakeChannel:
            def read_announcements(self):
                return [
                    {
                        "request_id": "abc",
                        "summary": "run pytest",
                        "expires_at": 9999999999.0,
                        "operation_digest": "deadbeef",
                        "kind": "exact",
                    }
                ]

        class FakeRuntime:
            service = FakeService()
            channel = FakeChannel()

        def failing_channel(*args, **kwargs):
            class _C:
                def write_decision(self, **kw):
                    raise RuntimeError("/secret/path/key.sqlite locked")

            return _C()

        monkeypatch.setattr(bridge, "_load_runtime", lambda: FakeRuntime())
        monkeypatch.setattr(bridge, "_load_channel", failing_channel)

        reply = bridge.decide_shell_request(123, "abc", "approve")
        assert "Nothing was approved" in reply
        # Internal paths never reach the chat.
        assert "/secret/path" not in reply
