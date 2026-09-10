"""Tests for the WebSocket server readiness behavior."""

import asyncio
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.models import ServerState
from server.websocket_server import WebSocketServer


class TestExtensionReadyEvent:
    """Test that the extension ready event lifecycle works correctly."""

    def test_ready_event_initially_not_set(self):
        """A fresh WebSocketServer has the ready event unset."""
        state = ServerState()
        ws = WebSocketServer(state)
        assert ws._extension_ready_event.is_set() is False

    def test_ready_event_set_on_connection(self):
        """The ready event is set when the extension connects."""
        state = ServerState()
        ws = WebSocketServer(state)
        # Simulate connection
        state.extension_connected = True
        ws._extension_ready_event.set()
        assert ws._extension_ready_event.is_set() is True

    def test_ready_event_recreated_on_disconnect(self):
        """A new ready event is created (unset) on disconnect."""
        state = ServerState()
        ws = WebSocketServer(state)
        # Simulate connection
        ws._extension_ready_event.set()
        old_event = ws._extension_ready_event
        # Simulate disconnect — new event is created
        ws._extension_ready_event = asyncio.Event()
        assert ws._extension_ready_event.is_set() is False
        assert ws._extension_ready_event is not old_event

    def test_wait_for_extension_ready_already_connected(self):
        """wait_for_extension_ready returns immediately when already connected."""
        state = ServerState()
        ws = WebSocketServer(state)
        ws._extension_ready_event.set()

        async def _test():
            return await asyncio.wait_for(
                ws._extension_ready_event.wait(), timeout=0.1
            )

        result = asyncio.run(_test())
        assert result is True

    def test_wait_for_extension_ready_times_out(self):
        """wait_for_extension_ready times out when extension never connects."""
        state = ServerState()
        ws = WebSocketServer(state)
        # Event is NOT set (extension not connected)

        async def _test():
            await asyncio.wait_for(
                ws._extension_ready_event.wait(), timeout=0.01
            )

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_test())

    def test_wait_for_extension_ready_succeeds_after_set(self):
        """wait_for_extension_ready returns when event is set during wait."""
        state = ServerState()
        ws = WebSocketServer(state)

        async def _test():
            async def set_after_delay():
                await asyncio.sleep(0.05)
                ws._extension_ready_event.set()

            asyncio.create_task(set_after_delay())
            return await asyncio.wait_for(
                ws._extension_ready_event.wait(), timeout=1.0
            )

        result = asyncio.run(_test())
        assert result is True

    def test_wait_for_extension_ready_method(self):
        """The public wait_for_extension_ready method works correctly."""
        state = ServerState()
        ws = WebSocketServer(state)

        # Already connected → returns True quickly
        ws._extension_ready_event.set()
        result = asyncio.run(ws.wait_for_extension_ready(timeout=1.0))
        assert result is True

    def test_wait_for_extension_ready_method_timeout(self):
        """The public wait_for_extension_ready method times out correctly."""
        state = ServerState()
        ws = WebSocketServer(state)
        # Event NOT set

        result = asyncio.run(ws.wait_for_extension_ready(timeout=0.01))
        assert result is False
