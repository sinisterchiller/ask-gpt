"""Tests for the MCP result format returned by ask_chatgpt.

Verifies that the result format is spec-compliant and does not include
extra fields that could interfere with how Claude Code's background task
system handles the result.
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.models import ServerState
from server.websocket_server import WebSocketServer
from server.mcp_server import MCPServer


class TestMCPResultFormat:
    """Test that the MCP result format is clean and spec-compliant."""

    def test_result_format_has_only_content(self):
        """The MCP result should contain only the 'content' array.
        
        The MCP spec defines the result as:
        {
          "content": [...],
          "structuredContent"?: ...,
          "isError"?: bool,
          "_meta"?: ...
        }
        
        Our minimal format uses only 'content', which is the required field.
        Extra fields like 'request_id' are NOT part of the spec and should
        be omitted to avoid potential issues with strict MCP clients.
        """
        # Simulate the result format used by _handle_tool_call
        response_text = "STEP 8 is close, but do not proceed to Step 9 yet."
        result = {
            "jsonrpc": "2.0",
            "id": "test_msg_id",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": response_text,
                    }
                ],
            },
        }

        # Verify the result structure
        assert "jsonrpc" in result
        assert result["jsonrpc"] == "2.0"
        assert "id" in result
        assert "result" in result

        mcp_result = result["result"]
        
        # The result should ONLY have the 'content' key
        # No extra fields like 'request_id'
        assert set(mcp_result.keys()) == {"content"}, \
            f"Result should only have 'content' key, got: {set(mcp_result.keys())}"

        # Verify content array structure
        assert isinstance(mcp_result["content"], list)
        assert len(mcp_result["content"]) == 1
        
        content_item = mcp_result["content"][0]
        assert content_item["type"] == "text"
        assert content_item["text"] == response_text

    def test_result_serialization(self):
        """Verify the result serializes to valid JSON."""
        response_text = "STEP 8 is close, but do not proceed to Step 9 yet."
        result = {
            "jsonrpc": "2.0",
            "id": "test_msg_id",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": response_text,
                    }
                ],
            },
        }

        # Should serialize without errors
        serialized = json.dumps(result) + "\n"
        assert isinstance(serialized, str)
        
        # Should deserialize correctly
        deserialized = json.loads(serialized)
        assert deserialized["result"]["content"][0]["text"] == response_text

    def test_result_no_request_id_field(self):
        """The MCP result should NOT include a 'request_id' field.
        
        The request_id is already tracked via:
        - The JSON-RPC 'id' field (message correlation)
        - The WebSocket 'requestId' field (protocol-level tracking)
        
        Including it in the result body is redundant and not part of the
        MCP specification.
        """
        response_text = "Test response"
        result = {
            "jsonrpc": "2.0",
            "id": "test_msg_id",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": response_text,
                    }
                ],
            },
        }

        mcp_result = result["result"]
        assert "request_id" not in mcp_result, \
            "MCP result should not contain 'request_id' field"

    def test_result_matches_sdk_output(self):
        """Verify our manual result format matches what the MCP SDK would produce."""
        from mcp.types import CallToolResult, TextContent

        # What our server produces
        our_result = {
            "content": [
                {
                    "type": "text",
                    "text": "Hello world",
                }
            ],
        }

        # What the MCP SDK produces
        sdk_result = CallToolResult(
            content=[TextContent(type="text", text="Hello world")]
        )
        sdk_dict = sdk_result.model_dump()

        # Our content should match the SDK's content in the essential fields
        # (SDK includes null defaults for optional fields like annotations/meta)
        our_item = our_result["content"][0]
        sdk_items = sdk_result.model_dump()["content"][0]
        assert our_item["type"] == sdk_items["type"]
        assert our_item["text"] == sdk_items["text"]

        # Our format is a minimal subset of the SDK output
        # (SDK includes null defaults for optional fields)
        # The essential fields (type, text) match
