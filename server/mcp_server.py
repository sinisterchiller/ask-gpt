"""MCP server exposing the ask_chatgpt tool to Claude Code.

Uses the MCP stdio transport. Claude Code invokes the tool, which
queues a request, waits for the response from the Chrome extension,
and returns it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

from .config import (
    EXTENSION_READY_TIMEOUT,
    MAX_PROMPT_SIZE,
    MAX_QUEUE_SIZE,
    REQUEST_TIMEOUT_MS,
)

# Directory for persisting ChatGPT responses (for recovery when
# Claude Code's background task system drops the result payload).
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
from .models import BridgeRequest, RequestState, ServerState
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

    # Maximum number of persisted results to retain (cleanup policy).
    # Oldest results are purged when this limit is exceeded.
    _MAX_PERSISTED_RESULTS = 50

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
        self._uses_add_reader: bool = False
        # Track the most recently completed request for latest-result recovery.
        # This is essential when Claude Code's background task system drops
        # the request_id from completion metadata.
        self._latest_result_id: str | None = None
        # Ensure results directory exists
        os.makedirs(RESULTS_DIR, exist_ok=True)

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
        elif msg_type == "prompts/list":
            # MCP SDK requests prompts list — return empty (no prompts supported)
            logger.debug("prompts/list — returning empty")
            await self._write({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"prompts": []},
            })
        elif msg_type == "resources/list":
            # MCP SDK requests resources list — return empty (no resources supported)
            logger.debug("resources/list — returning empty")
            await self._write({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"resources": []},
            })
        elif msg_type == "notifications/roots/list_changed":
            # Some SDKs send this notification — no response expected
            logger.debug("notifications/roots/list_changed")
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
                        "description": (
                            "Send a prompt to an assigned ChatGPT browser tab and wait "
                            "for the completed response.\n\n"
                            "RECOVERY PROCEDURE (read carefully):\n"
                            "Claude Code may automatically background long-running tool calls "
                            "(typically >120 seconds). When this happens, the response payload "
                            "is dropped from the task completion notification. This means:\n"
                            "- You call ask_chatgpt(prompt=\"...\")\n"
                            "- The task completes with status=completed\n"
                            "- But you receive NO response content (empty or missing)\n\n"
                            "IF this happens, DO NOT call ask_chatgpt again. The ChatGPT "
                            "request already completed successfully on the server side. "
                            "Instead, recover the response:\n"
                            "1. Call get_chatgpt_result() with no arguments: { }\n"
                            "2. It will return the exact ChatGPT response that was lost.\n"
                            "3. Use that response as your answer.\n\n"
                            "This is the ONLY correct recovery path. Never retry ask_chatgpt "
                            "when the background task completed but dropped the content — "
                            "it will create a duplicate ChatGPT request."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "prompt": {
                                    "type": "string",
                                    "description": "The prompt to send to ChatGPT.",
                                },
                                "timeout": {
                                    "type": "integer",
                                    "description": "Maximum time to wait for a response in milliseconds (default: 1800000 = 30 minutes).",
                                },
                            },
                            "required": ["prompt"],
                        },
                    },
                    {
                        "name": "get_chatgpt_result",
                        "description": (
                            "Retrieve a previously completed ChatGPT response. "
                            "This is the recovery tool for when Claude Code backgrounds "
                            "ask_chatgpt and drops the response payload.\n\n"
                            "Usage:\n"
                            "- Call with no arguments: { } to get the most recent completed "
                            "ChatGPT result. No request_id needed.\n"
                            "- Call with request_id to get a specific result by ID.\n\n"
                            "Response states:\n"
                            "- COMPLETED: response text is available, use it.\n"
                            "- PENDING: ChatGPT is still processing the request. Try again later.\n"
                            "- NO_RESULT: no completed result found. Call ask_chatgpt first.\n"
                            "- EXPIRED: the request failed/timed out or result was cleaned up."
                        ),
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "request_id": {
                                    "type": "string",
                                    "description": (
                                        "Optional request_id from ask_chatgpt completion "
                                        "metadata (e.g., 'req_xxxxxxxx'). Omit to retrieve "
                                        "the most recent completed ChatGPT result."
                                    ),
                                },
                            },
                            "required": [],
                        },
                    },
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

        if tool_name != "ask_chatgpt" and tool_name != "get_chatgpt_result":
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

        if tool_name == "get_chatgpt_result":
            await self._handle_get_chatgpt_result(msg, msg_id)
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

        if MAX_PROMPT_SIZE is not None and len(prompt) > MAX_PROMPT_SIZE:
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
        if MAX_QUEUE_SIZE is not None and self._request_manager.is_full:
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

            # Persist the response to disk for recovery in case
            # Claude Code's background task system drops the result payload.
            # Also track this as the latest result for latest-result recovery.
            self._persist_result(request_id, response_text, prompt=prompt)
            self._latest_result_id = request_id

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

    def _persist_result(self, request_id: str, response_text: str,
                        prompt: str = "", error_code: str = "",
                        status: str = "completed") -> None:
        """Persist a ChatGPT response to disk for recovery.

        The result is stored in a JSON file named by request_id with
        rich metadata (created_at, completed_at, status, prompt, error_code).
        This provides a durable backup in case Claude Code's background
        task system drops the result payload.

        Args:
            request_id: The bridge request ID.
            response_text: The ChatGPT response text.
            prompt: The original prompt sent to ChatGPT.
            error_code: Error code if the request failed (empty if succeeded).
            status: One of "completed", "failed", "timed_out", "cancelled".
        """
        try:
            result_path = os.path.join(RESULTS_DIR, f"{request_id}.json")
            result_data = {
                "request_id": request_id,
                "prompt": prompt,
                "response": response_text,
                "status": status,
                "error_code": error_code,
                "created_at": time.time(),
                "completed_at": time.time(),
                "persisted_at": time.time(),
            }
            with open(result_path, "w") as f:
                json.dump(result_data, f)
            # Enforce retention policy after writing
            self._purge_old_results()
            logger.info("[MCP] Persisted result for %s (%d chars, status=%s) to %s",
                        request_id, len(response_text), status, result_path)
        except Exception:
            logger.exception("[MCP] Failed to persist result for %s", request_id)

    def _purge_old_results(self) -> None:
        """Enforce retention policy: keep at most _MAX_PERSISTED_RESULTS results.

        Purges the oldest results (by filename/mtime) when the limit is exceeded.
        """
        try:
            existing = [
                f for f in os.listdir(RESULTS_DIR)
                if f.endswith(".json")
            ]
            if len(existing) <= self._MAX_PERSISTED_RESULTS:
                return
            # Sort by modification time (oldest first) and remove excess
            existing_with_mtime = []
            for fname in existing:
                fpath = os.path.join(RESULTS_DIR, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    existing_with_mtime.append((mtime, fname))
                except OSError:
                    pass
            existing_with_mtime.sort()  # oldest first
            to_remove = existing_with_mtime[:len(existing) - self._MAX_PERSISTED_RESULTS]
            for _mtime, fname in to_remove:
                fpath = os.path.join(RESULTS_DIR, fname)
                try:
                    os.remove(fpath)
                    logger.info("[MCP] Purged old result: %s", fname)
                except OSError:
                    pass
        except Exception:
            logger.exception("[MCP] Failed to purge old results")

    def _get_persisted_result(self, request_id: str) -> dict[str, Any]:
        """Retrieve a persisted ChatGPT response with metadata.

        Returns a dict with keys:
            found: bool - True if the result exists
            status: str - "completed", "failed", "timed_out", etc.
            response: str - the response text (empty if not found/error)
            error: str - error message if not found
        """
        try:
            result_path = os.path.join(RESULTS_DIR, f"{request_id}.json")
            if not os.path.exists(result_path):
                return {
                    "found": False,
                    "status": "NO_RESULT",
                    "response": "",
                    "error": (
                        f"Result not found for request_id '{request_id}'. "
                        "The request may not have completed yet, or the result "
                        "may have been cleaned up."
                    ),
                }
            with open(result_path, "r") as f:
                result_data = json.load(f)
            return {
                "found": True,
                "status": result_data.get("status", "completed"),
                "response": result_data.get("response", ""),
                "error": "",
            }
        except json.JSONDecodeError:
            return {
                "found": False,
                "status": "EXPIRED",
                "response": "",
                "error": f"Result file for '{request_id}' is corrupted.",
            }
        except Exception as e:
            return {
                "found": False,
                "status": "EXPIRED",
                "response": "",
                "error": f"Error reading result for '{request_id}': {e}",
            }

    def _get_latest_persisted_result(self) -> dict[str, Any]:
        """Get the most recently completed ChatGPT result.

        Looks through persisted result files and returns the one with the
        latest completed_at timestamp that has status "completed".

        Returns a dict with keys:
            found: bool
            status: str - "COMPLETED", "PENDING", "NO_RESULT", "EXPIRED"
            response: str
            request_id: str - the request_id of the result found
            error: str
        """
        try:
            existing = [
                f for f in os.listdir(RESULTS_DIR)
                if f.endswith(".json")
            ]
            if not existing:
                return {
                    "found": False,
                    "status": "NO_RESULT",
                    "response": "",
                    "request_id": "",
                    "error": "No ChatGPT results have been persisted yet. "
                             "Call ask_chatgpt and wait for completion.",
                }

            # Load all results and find the latest completed one
            latest = None
            latest_time = 0.0
            for fname in existing:
                fpath = os.path.join(RESULTS_DIR, fname)
                try:
                    with open(fpath, "r") as f:
                        data = json.load(f)
                    completed_at = data.get("completed_at", 0)
                    status = data.get("status", "")
                    # Prefer completed results; fall back to most recent
                    if status == "completed" and completed_at > latest_time:
                        latest = data
                        latest_time = completed_at
                    elif status != "completed" and completed_at > latest_time:
                        # No completed result yet — return the most recent
                        # (it may still be pending)
                        latest = data
                        latest_time = completed_at
                except (json.JSONDecodeError, OSError):
                    continue

            if latest is None:
                return {
                    "found": False,
                    "status": "NO_RESULT",
                    "response": "",
                    "request_id": "",
                    "error": "No valid ChatGPT results found.",
                }

            request_id = latest.get("request_id", "")
            status = latest.get("status", "completed")
            response = latest.get("response", "")
            error_code = latest.get("error_code", "")

            if status == "completed":
                return {
                    "found": True,
                    "status": "COMPLETED",
                    "response": response,
                    "request_id": request_id,
                    "error": "",
                }
            elif status in ("failed", "timed_out"):
                return {
                    "found": True,
                    "status": "EXPIRED",
                    "response": "",
                    "request_id": request_id,
                    "error": (
                        f"CHATGPT_RESULT_EXPIRED: The most recent ChatGPT "
                        f"request ({request_id}) ended with status '{status}'"
                        + (f" ({error_code})" if error_code else "")
                        + ". No successful result is available."
                    ),
                }
            else:
                # Pending or unknown state
                return {
                    "found": True,
                    "status": "PENDING",
                    "response": "",
                    "request_id": request_id,
                    "error": (
                        f"CHATGPT_RESULT_PENDING: The most recent ChatGPT "
                        f"request ({request_id}) is still processing "
                        f"(state: {status}). Try again later."
                    ),
                }

        except Exception as e:
            return {
                "found": False,
                "status": "NO_RESULT",
                "response": "",
                "request_id": "",
                "error": f"Error scanning results directory: {e}",
            }

    async def _handle_get_chatgpt_result(self, msg: dict[str, Any], msg_id: Any) -> None:
        """Handle the get_chatgpt_result tool call.

        Supports two modes:
        - With request_id: look up the specific result by ID.
        - Without request_id: return the most recent completed result
          (for recovery when Claude Code backgrounded ask_chatgpt and
           dropped the request_id from completion metadata).
        """
        params = msg.get("params", {})
        arguments = params.get("arguments", {})
        request_id = arguments.get("request_id", "")

        if request_id:
            # Specific request lookup
            info = self._get_persisted_result(request_id)
            if info["found"]:
                result = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": info["response"],
                            }
                        ],
                    },
                }
            else:
                result = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"CHATGPT_RESULT_NOT_RETRIEVED: {info['error']}",
                            }
                        ],
                        "isError": True,
                    },
                }
        else:
            # Latest-result recovery — no request_id needed
            info = self._get_latest_persisted_result()
            if info["found"] and info["status"] == "COMPLETED":
                result = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": info["response"],
                            }
                        ],
                    },
                }
            else:
                # PENDING, NO_RESULT, or EXPIRED
                result = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"CHATGPT_{info['status']}: {info['error']}",
                            }
                        ],
                        "isError": True,
                    },
                }

        await self._write(result)

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
        """Run the MCP server, reading from stdin.

        Uses loop.add_reader() for non-blocking stdin I/O so that
        cancellation and shutdown don't leave a blocked executor thread.
        Falls back to run_in_executor for pipe stdin (add_reader only
        works with regular files/TTYs, not pipes).
        """
        logger.info("[MCP] process started")

        # Periodic cleanup of expired requests
        self._cleanup_task: asyncio.Task[None] | None = None
        try:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        except Exception:
            logger.exception("[MCP] Failed to start cleanup loop")

        loop = asyncio.get_event_loop()
        stdin_fd = sys.stdin.fileno()

        # Check if we can use add_reader (works for TTYs/regular files,
        # not for pipes — pipes raise OSError on macOS).
        self._uses_add_reader = False
        try:
            loop.add_reader(stdin_fd, lambda: None)
            loop.remove_reader(stdin_fd)
            self._uses_add_reader = True
        except OSError:
            self._uses_add_reader = False

        if self._uses_add_reader:
            # Buffered line reading state
            _buf = ""

            def _stdin_handler() -> None:
                nonlocal _buf
                try:
                    data = os.read(stdin_fd, 8192)
                except OSError:
                    loop.remove_reader(stdin_fd)
                    return
                if not data:
                    loop.remove_reader(stdin_fd)
                    return
                _buf += data.decode("utf-8", errors="replace")
                while "\n" in _buf:
                    line, _buf = _buf.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            asyncio.ensure_future(self.handle_message(line))
                        except Exception:
                            logger.exception("Error handling stdin message")

            loop.add_reader(stdin_fd, _stdin_handler)

            # Wait until the cleanup task is cancelled (on shutdown).
            # The add_reader callback runs independently on the event loop.
            try:
                if self._cleanup_task:
                    await self._cleanup_task
            except asyncio.CancelledError:
                pass
        else:
            # Pipe stdin — fall back to run_in_executor.
            # The caller (main()) must explicitly cancel the MCP task
            # and shut down the executor before returning.
            while True:
                try:
                    line = await loop.run_in_executor(None, sys.stdin.readline)
                except (EOFError, OSError):
                    logger.info("EOF reading from stdin, shutting down")
                    break

                line = line.strip()
                if not line:
                    logger.info("Empty line from stdin (EOF), shutting down")
                    break

                try:
                    await self.handle_message(line)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Unhandled error in message handling — continuing")

    async def _cleanup_loop(self) -> None:
        """Periodically clean up expired requests."""
        while True:
            await asyncio.sleep(10)
            expired = self._request_manager.cleanup_expired()
            if expired:
                logger.info("Cleaned up %d expired requests", len(expired))
