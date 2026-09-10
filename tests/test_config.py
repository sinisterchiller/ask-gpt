"""Tests for config and models."""

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import config
from server.models import BridgeRequest, RequestState, ServerState


class TestGetToken:
    def test_returns_string(self):
        # Just verify it returns a non-empty string
        token = config.get_token()
        assert isinstance(token, str)
        assert len(token) > 0

    def test_consistent(self):
        token1 = config.get_token()
        token2 = config.get_token()
        assert token1 == token2


class TestServerState:
    def test_defaults(self):
        state = ServerState()
        assert state.extension_connected is False
        assert state.bridge_enabled is True
        assert state.assigned_tab_id is None
        assert state.assigned_tab_url == ""
        assert len(state.pending_requests) == 0
        assert len(state.queue) == 0
        assert state.active_request is None


class TestBridgeRequest:
    def test_not_expired_initially(self):
        req = BridgeRequest(
            request_id="req_1",
            prompt="Hello",
            timeout_ms=60000,
        )
        assert req.is_expired() is False

    def test_expired_after_timeout(self):
        req = BridgeRequest(
            request_id="req_1",
            prompt="Hello",
            timeout_ms=50,
        )
        # Artificially age the request
        req.created_at = req.created_at - 10  # 10 seconds older
        assert req.is_expired() is True

    def test_not_expired_with_large_timeout(self):
        req = BridgeRequest(
            request_id="req_1",
            prompt="Hello",
            timeout_ms=3600000,
        )
        assert req.is_expired() is False
