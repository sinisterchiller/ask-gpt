"""End-to-end tests for the get_chatgpt_result recovery tool.

Tests both modes:
1. With request_id: look up specific result
2. Without request_id: latest-result recovery
"""

import asyncio
import json
import os
import sys
import tempfile
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.mcp_server import MCPServer
from server.models import ServerState
from server.websocket_server import WebSocketServer


class TestRecoveryToolModes:
    """Test the get_chatgpt_result tool in both modes."""

    def setup_method(self):
        """Create a temporary results directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self.tmpdir

    def teardown_method(self):
        """Clean up temp directory."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _capture_tool_call(self, tool_name, arguments):
        """Helper to call a tool and capture its output."""
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        msg = {
            "jsonrpc": "2.0",
            "id": "test_id",
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        async def run():
            mcp = self._mcp
            await mcp._handle_tool_call(msg, "test_id")

        asyncio.run(run())

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        return json.loads(output.strip())

    def test_recovery_with_request_id(self):
        """Recovery with request_id returns the specific result."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        self._mcp._persist_result("req_specific_123", "Specific response content")

        result = self._capture_tool_call(
            "get_chatgpt_result",
            {"request_id": "req_specific_123"},
        )

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert result["result"]["content"][0]["text"] == "Specific response content"
        assert "isError" not in result["result"]

    def test_recovery_without_request_id_latest(self):
        """Recovery without request_id returns the latest completed result."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        self._mcp._persist_result("req_first", "First response")
        import time
        time.sleep(0.01)
        self._mcp._persist_result("req_latest", "Latest response content")

        result = self._capture_tool_call("get_chatgpt_result", {})

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert result["result"]["content"][0]["text"] == "Latest response content"
        assert "isError" not in result["result"]

    def test_recovery_no_results(self):
        """Recovery with no results returns NO_RESULT."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        result = self._capture_tool_call("get_chatgpt_result", {})

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert "CHATGPT_NO_RESULT" in result["result"]["content"][0]["text"]
        assert result["result"].get("isError") is True

    def test_recovery_missing_specific_result(self):
        """Recovery with non-existent request_id returns NOT_RETRIEVED."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        result = self._capture_tool_call(
            "get_chatgpt_result",
            {"request_id": "req_does_not_exist"},
        )

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert "CHATGPT_RESULT_NOT_RETRIEVED" in result["result"]["content"][0]["text"]
        assert result["result"].get("isError") is True

    def test_recovery_with_non_completed_result(self):
        """Recovery returns EXPIRED when latest result is failed."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        self._mcp._persist_result(
            "req_failed",
            "Failed response",
            status="failed",
            error_code="CHATGPT_TIMEOUT",
        )

        result = self._capture_tool_call("get_chatgpt_result", {})

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert "CHATGPT_EXPIRED" in result["result"]["content"][0]["text"]
        assert result["result"].get("isError") is True

    def test_recovery_prefers_completed_over_failed(self):
        """Recovery prefers completed result even if older."""
        state = ServerState()
        ws = WebSocketServer(state)
        self._mcp = MCPServer(state, ws)

        import time
        # First, a failed result
        self._mcp._persist_result(
            "req_failed",
            "Failed response",
            status="failed",
            error_code="CHATGPT_TIMEOUT",
        )
        time.sleep(0.01)
        # Then, a completed result
        self._mcp._persist_result("req_ok", "Success response")

        result = self._capture_tool_call("get_chatgpt_result", {})

        assert "result" in result
        assert result["result"]["content"][0]["type"] == "text"
        assert result["result"]["content"][0]["text"] == "Success response"
        assert "isError" not in result["result"]
