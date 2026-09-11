"""MCP server exposing the ask_chatgpt tool to Claude Code.

Uses the MCP stdio transport. Claude Code invokes the tool, which
queues a request, waits for the response from the Chrome extension,
and returns it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

from .config import (
    EXTENSION_READY_TIMEOUT,
    MAX_PROMPT_SIZE,
    MAX_QUEUE_SIZE,
    REQUEST_TIMEOUT_MS,
)
from .models import RequestState, ServerState
from .protocol import (
    ErrorCode,
    encode,
    generate_request_id,
    make_error,
    parse_message,
)
from .request_manager import RequestManager
from .websocket_server import WebSocketServer

logger = logging.getLogger("chatgpt-bridge.mcp")

# MCP protocol message types
MSG_INITIALIZE = "initialize"
MSG_MESSAGE = "message"
MSG_TOOL_LIST = "tools/list"
MSG_TOOL_CALL = "tools/call"
MSG_RESULT = "result"
MSG_ERROR_MCP = "error"
MSG_NOTIFICATION = "notification"
MSG_LOG_MESSAGE = "logging/message"


class MCPServer:
    """MCP stdio server that exposes ask_chatgpt."""

    def __init__(self, state: ServerState, ws_server: WebSocketServer) -> None:
        self._state = state
        self._ws_server = ws_server
        self._request_manager = RequestManager(
            on_request_complete=self._on_request_complete
        )
        self._state.request_manager = self._request_manager
        self._message_id = 0
        self._initialized = False
        self._client_capabilities: dict[str, Any] = {}

    # --- MCP message handling ---

    async def handle_message(self, raw: str) -> None:
        """Handle an incoming MCP message from Claude Code (via stdio)."""
        msg = parse_message(raw)
        if msg is None:
            logger.warning("Invalid MCP JSON")
            return

        msg_type = msg.get("method") or msg.get("type")
        msg_id = msg.get("id")

        logger.debug("MCP inbound: method=%s id=%s", msg_type, msg_id)

        if msg_type == MSG_INITIALIZE:
            await self._handle_initialize(msg_id)
        elif msg_type == MSG_TOOL_LIST:
            await self._handle_tool_list(msg_id)
        elif msg_type == MSG_TOOL_CALL:
            await self._handle_tool_call(msg, msg_id)
        elif msg_type == "notifications/initialized":
            # SDK sends this notification after initialize — no response expected
            logger.debug("Received notifications/initialized")
        elif msg_type == "logging/setLevel":
            # Inspector uses this to set log level — no response expected
            logger.debug("Received logging/setLevel")
        elif msg_type == "logging/message":
            # Some clients send logging messages — no response expected
            logger.debug("Received logging/message")
        elif msg_type is None:
            # Notification without a method field — ignore (no response)
            pass
        else:
            # Only send responses to requests that have an id
            if msg_id is not None:
                logger.warning("Unknown method: %s, id=%s", msg_type, msg_id)
                error_resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {msg_type}",
                    },
                }
                await self._write(error_resp)

    async def _handle_initialize(self, msg_id: Any) -> None:
        """Handle MCP initialize handshake."""
        self._initialized = True
        logger.info("[MCP] initialization complete")
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {
                        "listChanged": False,
                    },
                    "experimental": {},
                    "prompts": {
                        "listChanged": False,
                    },
                    "resources": {
                        "subscribe": False,
                        "listChanged": False,
                    },
                    "logging": {},
                },
                "serverInfo": {
                    "name": "chatgpt-bridge",
                    "version": "0.1.0",
                },
            },
        }
        await self._write(response)

    async def _handle_tool_list(self, msg_id: Any) -> None:
        """List available tools."""
        logger.info("[MCP] tools/list")
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "tools": [
                    {
                        "name": "ask_chatgpt",
                        "description": "Send a prompt to an assigned ChatGPT browser tab and wait for the completed response.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "description": "The prompt to send to ChatGPT.",
                                },
                                "timeout": {
                                    "type": "integer",
                                    "description": "Maximum time to wait for a response in milliseconds (default: 300000 = 5 minutes).",
                                },
                            },
                            "required": ["prompt"],
                        },
                    }
                ]
            },
        }
        await self._write(response)

    async def _handle_tool_call(self, msg: dict[str, Any], msg_id: Any) -> None:
        """Handle a tool call — the ask_chatgpt tool."""
        params = msg.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        logger.info("Tool call: name=%s args=%s", tool_name, arguments)

        if tool_name != "ask_chatgpt":
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Unknown tool: {tool_name}",
                },
            }
            await self._write(error_resp)
            return

        prompt = arguments.get("prompt", "")
        timeout_ms = arguments.get("timeout", REQUEST_TIMEOUT_MS)

        logger.info(
            "[MCP] ask_chatgpt req=%s entered: prompt_len=%d, extension_connected=%s, timeout_ms=%d",
            msg_id,
            len(prompt),
            self._state.extension_connected,
            timeout_ms,
        )

        if not prompt:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": "prompt is required and must be a non-empty string",
                },
            }
            await self._write(error_resp)
            return

        if len(prompt) > MAX_PROMPT_SIZE:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32602,
                    "message": f"Prompt exceeds maximum size of {MAX_PROMPT_SIZE} characters",
                },
            }
            await self._write(error_resp)
            return

        # Wait for extension to be ready (tolerates cold-start race).
        # The extension normally reconnects within 1-2s of the server starting.
        # This is separate from the ChatGPT response timeout below.
        ready = await self._ws_server.wait_for_extension_ready(EXTENSION_READY_TIMEOUT)
        if not ready:
            logger.warning("Extension not ready — returning CHATGPT_BRIDGE_OFFLINE")
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32001,
                    "message": ErrorCode.CHATGPT_BRIDGE_OFFLINE.value,
                },
            }
            await self._write(error_resp)
            return

        # Check extension connection (redundant after wait_for_extension_ready,
        # but kept as a safety net in case the connection dropped between
        # the readiness check and here)
        if not self._state.extension_connected:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32001,
                    "message": ErrorCode.CHATGPT_BRIDGE_OFFLINE.value,
                },
            }
            await self._write(error_resp)
            return

        # Check bridge enabled
        if not self._state.bridge_enabled:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32002,
                    "message": ErrorCode.BRIDGE_DISABLED.value,
                },
            }
            await self._write(error_resp)
            return

        # Check assigned tab
        if not self._state.assigned_tab_id:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32003,
                    "message": ErrorCode.NO_CHATGPT_TAB.value,
                },
            }
            await self._write(error_resp)
            return

        # Check queue size
        if self._request_manager.is_full:
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32004,
                    "message": f"Queue full ({MAX_QUEUE_SIZE} requests)",
                },
            }
            await self._write(error_resp)
            return

        # Create request
        request = self._request_manager.create_request(prompt, timeout_ms)
        request_id = request.request_id

        logger.info("[MCP] Created request %s, state=%s, active=%s, future=%s",
                    request_id, request.state,
                    self._request_manager.active.request_id if self._request_manager.active else None,
                    request.future)

        # Create a future to await
        loop = asyncio.get_event_loop()
        request.future = loop.create_future()

        logger.info("[MCP] Created future for request %s: %s (is_same=%s)",
                    request_id, request.future, request.future is request.future)

        # The request may have been auto-activated by create_request,
        # or it may be queued (if another request is active).
        # In either case, if it's active, dispatch it now.
        # If it's queued, we'll wait for the active request to finish.
        if self._request_manager.active and self._request_manager.active.request_id == request_id:
            logger.info("[MCP] Request %s is ACTIVE — dispatching now", request_id)
            # This request was auto-activated — dispatch immediately.
            try:
                await self._ws_server.dispatch_prompt(request_id, prompt, timeout_ms)
                logger.info("Request %s dispatched to extension", request_id)
            except ConnectionError:
                request.state = RequestState.FAILED
                request.error_code = ErrorCode.EXTENSION_DISCONNECTED.value
                self._request_manager.complete_request(request_id, error_code=ErrorCode.EXTENSION_DISCONNECTED.value)
                error_resp = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32010,
                        "message": ErrorCode.EXTENSION_DISCONNECTED.value,
                    },
                }
                await self._write(error_resp)
                return
        else:
            logger.info("[MCP] Request %s is QUEUED — waiting for active request to finish", request_id)
            # Queued — wait with timeout (will be dispatched by _on_request_complete)
            pass

        # Wait for response or timeout
        logger.info("[MCP] About to await request.future for request %s (timeout=%.1fs)",
                    request_id, timeout_ms / 1000)
        try:
            await asyncio.wait_for(request.future, timeout=timeout_ms / 1000)
            logger.info("[MCP] request.future resolved for %s", request_id)
            logger.info("[REQ %s] MCP_RETURNING response_len=%d",
                        request_id, len(request.response) if request.response else 0)
            response_text = request.response

            result = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": response_text,
                        }
                    ],
                    "request_id": request_id,
                },
            }
            await self._write(result)
            logger.info("Request %s completed, response sent to Claude", request_id)

        except asyncio.TimeoutError:
            request.state = RequestState.TIMED_OUT
            self._request_manager.complete_request(request_id, error_code=ErrorCode.CHATGPT_TIMEOUT.value)
            elapsed = time.time() - request.created_at
            stage_info = ""
            if request.last_stage:
                stage_info = f" last_stage={request.last_stage} elapsed={elapsed:.1f}s"
            error_msg = ErrorCode.CHATGPT_TIMEOUT.value + stage_info
            error_resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32005,
                    "message": error_msg,
                },
            }
            await self._write(error_resp)
            logger.warning("Request %s timed out%s", request_id, stage_info)

    def _on_request_complete(self, request_id: str) -> None:
        """Called when a request completes — activate the next queued request."""
        logger.info("[MCP] _on_request_complete(%s) called, active=%s, queue_size=%d",
                    request_id,
                    self._request_manager.active.request_id if self._request_manager.active else None,
                    len(self._request_manager.queue))

        # If there's an active request, skip (shouldn't happen but safety)
        if self._request_manager.active:
            logger.warning("[MCP] _on_request_complete: active request still exists, skipping")
            return

        # Try to activate next
        next_req = self._request_manager.activate_next()
        if next_req:
            logger.info("[MCP] Activated next request %s", next_req.request_id)
            asyncio.create_task(self._dispatch_queued_request(next_req))
        else:
            logger.info("[MCP] No next request to activate")

    async def _dispatch_queued_request(self, request: BridgeRequest) -> None:
        """Dispatch a queued request to the extension."""
        try:
            await self._ws_server.dispatch_prompt(
                request.request_id, request.prompt, request.timeout_ms
            )
            logger.info("Queued request %s dispatched", request.request_id)
        except ConnectionError:
            request.state = RequestState.FAILED
            request.error_code = ErrorCode.EXTENSION_DISCONNECTED.value
            self._request_manager.complete_request(
                request.request_id, error_code=ErrorCode.EXTENSION_DISCONNECTED.value
            )

    async def _write(self, data: dict[str, Any]) -> None:
        """Write an MCP response to stdout (stdio)."""
        line = json.dumps(data) + "\n"
        sys.stdout.write(line)
        sys.stdout.flush()

    async def run(self) -> None:
        """Run the MCP server, reading from stdin."""
        logger.info("[MCP] process started")

        # Periodic cleanup of expired requests
        try:
            asyncio.create_task(self._cleanup_loop())
        except Exception:
            logger.exception("[MCP] Failed to start cleanup loop")

        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, OSError):
                logger.info("EOF reading from stdin, shutting down")
                break

            line = line.strip()
            if not line:
                # Empty line means EOF — stdin was closed
                logger.info("Empty line from stdin (EOF), shutting down")
                break

            try:
                await self.handle_message(line)
            except asyncio.CancelledError:
                # Request timeout cancelled this coroutine — do NOT propagate.
                # Clean up and continue processing the next message.
                logger.warning("Request cancelled/timeout — continuing server loop")
            except Exception:
                logger.exception("Unhandled error in message handling — continuing")

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired requests."""
        while True:
            await asyncio.sleep(10)
            expired = self._request_manager.cleanup_expired()
            if expired:
                logger.info("Cleaned up %d expired requests", len(expired))
