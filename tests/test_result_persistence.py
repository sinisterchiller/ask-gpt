"""Tests for the ChatGPT result persistence and recovery mechanism.

This mechanism provides a durable backup of ChatGPT responses in case
Claude Code's background task system drops the result payload.
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.mcp_server import MCPServer
from server.models import ServerState
from server.websocket_server import WebSocketServer


class TestResultPersistence:
    """Test that ChatGPT responses are persisted to disk."""

    def setup_method(self):
        """Create a temporary results directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        # Patch the RESULTS_DIR to use our temp directory
        import server.mcp_server as mcp_module
        self._original_results_dir = mcp_module.RESULTS_DIR
        mcp_module.RESULTS_DIR = self.tmpdir

    def teardown_method(self):
        """Restore the original RESULTS_DIR and clean up."""
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self._original_results_dir
        # Clean up temp directory
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_persist_result_creates_file(self):
        """Persisting a result creates a JSON file."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_test_123", "Hello world")

        result_path = os.path.join(self.tmpdir, "req_test_123.json")
        assert os.path.exists(result_path)

    def test_persist_result_content(self):
        """Persisted result contains the correct data."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        response_text = "STEP 8 is close, but do not proceed to Step 9 yet."
        mcp._persist_result("req_test_123", response_text)

        result_path = os.path.join(self.tmpdir, "req_test_123.json")
        with open(result_path, "r") as f:
            result_data = json.load(f)

        assert result_data["request_id"] == "req_test_123"
        assert result_data["response"] == response_text
        assert "persisted_at" in result_data
        assert "created_at" in result_data
        assert "completed_at" in result_data
        assert result_data["status"] == "completed"

    def test_persist_result_overwrites(self):
        """Persisting the same request_id overwrites the previous result."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_test_123", "First response")
        mcp._persist_result("req_test_123", "Second response")

        result_path = os.path.join(self.tmpdir, "req_test_123.json")
        with open(result_path, "r") as f:
            result_data = json.load(f)

        assert result_data["response"] == "Second response"

    def test_persist_result_with_prompt(self):
        """Persisting a result stores the prompt."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_test_123", "Response", prompt="Test prompt")

        result_path = os.path.join(self.tmpdir, "req_test_123.json")
        with open(result_path, "r") as f:
            result_data = json.load(f)

        assert result_data["prompt"] == "Test prompt"


class TestResultRecovery:
    """Test that persisted results can be retrieved."""

    def setup_method(self):
        """Create a temporary results directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self.tmpdir

    def teardown_method(self):
        """Restore the original RESULTS_DIR and clean up."""
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self._original_results_dir if hasattr(self, '_original_results_dir') else None
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_get_persisted_result_found(self):
        """Retrieving an existing result returns the content."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        response_text = "BACKGROUND_RESULT_TEST_483729 - Do not proceed to Step 9."
        mcp._persist_result("req_test_123", response_text)

        info = mcp._get_persisted_result("req_test_123")
        assert info["found"] is True
        assert info["response"] == response_text
        assert info["status"] == "completed"
        assert info["error"] == ""

    def test_get_persisted_result_not_found(self):
        """Retrieving a non-existent result returns not found info."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        info = mcp._get_persisted_result("req_nonexistent")
        assert info["found"] is False
        assert info["status"] == "NO_RESULT"
        assert "not found" in info["error"].lower()

    def test_get_persisted_result_empty_response(self):
        """Retrieving a result with empty response returns found=True."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_test_123", "")

        info = mcp._get_persisted_result("req_test_123")
        assert info["found"] is True
        assert info["response"] == ""
        assert info["status"] == "completed"


class TestLatestResultRecovery:
    """Test latest-result recovery (no request_id needed)."""

    def setup_method(self):
        """Create a temporary results directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self.tmpdir

    def teardown_method(self):
        """Clean up temp directory."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_latest_result_returns_completed(self):
        """Latest result returns the most recent completed response."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_first", "First response")
        time.sleep(0.01)
        mcp._persist_result("req_second", "Second response")

        info = mcp._get_latest_persisted_result()
        assert info["found"] is True
        assert info["status"] == "COMPLETED"
        assert info["response"] == "Second response"
        assert info["request_id"] == "req_second"

    def test_latest_result_no_results(self):
        """Latest result returns NO_RESULT when no results exist."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        info = mcp._get_latest_persisted_result()
        assert info["found"] is False
        assert info["status"] == "NO_RESULT"
        assert "no" in info["error"].lower()

    def test_latest_result_prefers_completed(self):
        """Latest result prefers completed over failed."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_failed", "Failed response", status="failed", error_code="CHATGPT_TIMEOUT")
        time.sleep(0.01)
        mcp._persist_result("req_completed", "Success response", status="completed")

        info = mcp._get_latest_persisted_result()
        assert info["found"] is True
        assert info["status"] == "COMPLETED"
        assert info["response"] == "Success response"

    def test_latest_result_expired_failed(self):
        """Latest result returns EXPIRED when only failed results exist."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        mcp._persist_result("req_failed", "Failed response", status="failed", error_code="CHATGPT_TIMEOUT")

        info = mcp._get_latest_persisted_result()
        assert info["found"] is True
        assert info["status"] == "EXPIRED"
        assert "expired" in info["error"].lower()

    def test_purge_old_results(self):
        """Retention policy purges oldest results when limit exceeded."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        # Set a low limit for testing
        original_limit = mcp._MAX_PERSISTED_RESULTS
        mcp._MAX_PERSISTED_RESULTS = 3

        for i in range(5):
            mcp._persist_result(f"req_{i}", f"Response {i}")
            time.sleep(0.01)

        # Should have at most 3 files (the retention limit)
        existing = [f for f in os.listdir(self.tmpdir) if f.endswith(".json")]
        assert len(existing) <= 3

        # The oldest results (req_0, req_1) should be gone
        assert not os.path.exists(os.path.join(self.tmpdir, "req_0.json"))
        assert not os.path.exists(os.path.join(self.tmpdir, "req_1.json"))

        mcp._MAX_PERSISTED_RESULTS = original_limit


class TestToolListIncludesRecovery:
    """Test that the tool list includes both ask_chatgpt and get_chatgpt_result."""

    def setup_method(self):
        """Create a temporary results directory for each test."""
        self.tmpdir = tempfile.mkdtemp()
        import server.mcp_server as mcp_module
        mcp_module.RESULTS_DIR = self.tmpdir

    def teardown_method(self):
        """Clean up temp directory."""
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tool_list_contains_both_tools(self):
        """The tool list includes both ask_chatgpt and get_chatgpt_result."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        # Manually call the tool list handler
        import asyncio
        import json
        from io import StringIO

        # Capture the written output
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        async def run():
            await mcp._handle_tool_list("test_msg_id")

        asyncio.run(run())

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        # Parse the JSON output
        result = json.loads(output.strip())
        tools = result["result"]["tools"]

        tool_names = [t["name"] for t in tools]
        assert "ask_chatgpt" in tool_names
        assert "get_chatgpt_result" in tool_names

    def test_get_chatgpt_result_tool_description(self):
        """The get_chatgpt_result tool has a descriptive description."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        import asyncio
        import json
        from io import StringIO

        old_stdout = sys.stdout
        sys.stdout = StringIO()

        async def run():
            await mcp._handle_tool_list("test_msg_id")

        asyncio.run(run())

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        result = json.loads(output.strip())
        tools = {t["name"]: t for t in result["result"]["tools"]}

        recovery_tool = tools["get_chatgpt_result"]
        assert "recover" in recovery_tool["description"].lower() or \
               "background" in recovery_tool["description"].lower()
        assert "request_id" in recovery_tool["inputSchema"]["properties"]

    def test_get_chatgpt_result_request_id_optional(self):
        """The get_chatgpt_result tool has request_id as optional."""
        state = ServerState()
        ws = WebSocketServer(state)
        mcp = MCPServer(state, ws)

        import asyncio
        import json
        from io import StringIO

        old_stdout = sys.stdout
        sys.stdout = StringIO()

        async def run():
            await mcp._handle_tool_list("test_msg_id")

        asyncio.run(run())

        output = sys.stdout.getvalue()
        sys.stdout = old_stdout

        result = json.loads(output.strip())
        tools = {t["name"]: t for t in result["result"]["tools"]}

        recovery_tool = tools["get_chatgpt_result"]
        # request_id should NOT be in required (it's optional)
        assert "request_id" not in recovery_tool["inputSchema"].get("required", [])
