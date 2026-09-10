"""WebSocket server for the ChatGPT Bridge.

Handles extension connections, message routing, and request lifecycle.
Binds only to 127.0.0.1 for security.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import websockets
from websockets.server import ServerConnection

from . import protocol
from .config import HOST, PORT, WS_PATH, get_token
from .models import RequestState, ServerState

logger = logging.getLogger("chatgpt-bridge.ws")


class WebSocketServer:
    """Manages WebSocket connections and message routing."""

    def __init__(self, state: ServerState) -> None:
        self._state = state
        self._state.ws_token = get_token()
        self._ws: ServerConnection | None = None
        self._server: Any = None
        self._ws_ready = asyncio.Event()
        self._disconnected_event = asyncio.Event()
        self._disconnected_event.set()
        # Event set when extension connects, cleared on disconnect.
        # Used by the MCP handler to wait for extension readiness
        # instead of failing immediately on first invocation.
        self._extension_ready_event: asyncio.Event = asyncio.Event()

    async def connect(self) -> ServerConnection:
        """Establish the WebSocket server."""
        if self._server is not None:
            return self._ws  # type: ignore[return-value]

        self._server = await websockets.serve(
            self._handler,
            HOST,
            PORT,
            process_request=self._process_request,
            max_size=2**20,  # 1MB max message
        )
        logger.info("WebSocket server listening on ws://%s:%d%s", HOST, PORT, WS_PATH)
        return self._ws  # type: ignore[return-value]

    async def _process_request(
        self, path: str, request_headers: Any
    ) -> tuple[Any, Any, Any] | None:
        """Validate path and token before WebSocket upgrade."""
        # Extract token from query string
        token = ""
        if "?" in path:
            query = path.split("?", 1)[1]
            for param in query.split("&"):
                if param.startswith("token="):
                    token = param[6:]

        # Validate token
        if token != self._state.ws_token:
            logger.warning("Authentication failed for path: %s", path)
            return (
                403,
                {"Content-Type": "text/plain"},
                b"Authentication required",
            )

        # Validate path
        if not path.startswith(WS_PATH):
            return (
                404,
                {"Content-Type": "text/plain"},
                b"Not found",
            )

        return None  # Continue with WebSocket upgrade

    async def _handler(self, ws: ServerConnection, path: str = "/ws") -> None:
        """Handle a single WebSocket connection."""
        logger.info("Extension connecting...")

        self._ws = ws
        self._state.extension_connected = True
        self._disconnected_event.clear()
        self._extension_ready_event.set()
        logger.info("Extension connected")

        # Send hello ack
        await self._send(protocol.make_hello_ack())

        try:
            async for raw in ws:
                await self._handle_message(raw)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Extension disconnected")
        finally:
            await self._on_disconnect()

    async def _handle_message(self, raw: str) -> None:
        """Process an incoming message from the extension."""
        msg = protocol.parse_message(raw)
        if msg is None:
            logger.warning("Invalid JSON received")
            await self._send_error(None, protocol.ErrorCode.INVALID_PROTOCOL_MESSAGE, "Invalid JSON")
            return

        msg_type = msg.get("type")

        if msg_type == protocol.MSG_TAB_CHANGED:
            tab_id = msg.get("tabId")
            url = msg.get("url", "")
            self._state.assigned_tab_id = tab_id
            self._state.assigned_tab_url = url
            logger.info("Tab changed: id=%s url=%s", tab_id, url)

        elif msg_type == protocol.MSG_ACCEPTED:
            request_id = msg.get("requestId")
            if request_id:
                logger.info("Request %s accepted by extension", request_id)

        elif msg_type == protocol.MSG_RESPONSE:
            if not protocol.validate_response(msg):
                logger.warning("Invalid response message")
                return
            request_id = msg["requestId"]
            response_text = msg["response"]
            url = msg.get("url", "")

            req = self._state.request_manager.get_request(request_id)
            if req and req.future and not req.future.done():
                req.response = response_text
                req.state = RequestState.COMPLETED
                req.future.set_result(response_text)
                self._state.request_manager.complete_request(request_id, response=response_text)
                logger.info("Request %s response received (%d chars)", request_id, len(response_text))
            else:
                logger.warning("Response for unknown request: %s", request_id)

        elif msg_type == protocol.MSG_ERROR:
            request_id = msg.get("requestId")
            error_code = msg.get("code", "UNKNOWN_ERROR")
            error_message = msg.get("message", "Unknown error")
            logger.warning("Extension error for %s: %s - %s", request_id, error_code, error_message)

            req = self._state.request_manager.get_request(request_id)
            if req and req.future and not req.future.done():
                req.error_code = error_code
                req.error_message = error_message
                req.state = RequestState.FAILED
                req.future.set_exception(RuntimeError(f"{error_code}: {error_message}"))
                self._state.request_manager.complete_request(
                    request_id, error_code=error_code
                )

        elif msg_type == protocol.MSG_PING:
            await self._send(protocol.make_pong())

        elif msg_type == protocol.MSG_HELLO:
            logger.info("Extension hello received")
            await self._send(protocol.make_hello_ack())

        else:
            logger.warning("Unknown message type: %s", msg_type)

    async def _on_disconnect(self) -> None:
        """Handle WebSocket disconnection."""
        self._state.extension_connected = False
        self._ws = None
        self._disconnected_event.set()
        # Create a fresh event so the next tool call waits for reconnection
        self._extension_ready_event = asyncio.Event()
        logger.info("Extension disconnected — cleaning up active request")

        active = self._state.request_manager.active
        if active:
            self._state.request_manager.fail_request(
                active.request_id,
                protocol.ErrorCode.EXTENSION_DISCONNECTED.value,
                "WebSocket disconnected during request",
            )

    async def _send(self, message: dict[str, Any]) -> None:
        """Send a message to the extension."""
        if self._ws is None or self._ws.state.value == 3:
            raise ConnectionError("WebSocket not connected")
        await self._ws.send(protocol.encode(message))

    async def _send_error(self, request_id: str | None, code: Any, message: str) -> None:
        """Send an error message to the extension."""
        if self._ws is None:
            return
        await self._send(protocol.make_error(request_id, code, message))

    async def dispatch_prompt(
        self, request_id: str, prompt: str, timeout_ms: int
    ) -> None:
        """Send a prompt to the extension."""
        import logging
        logger = logging.getLogger("chatgpt-bridge.ws")
        logger.info("Dispatching prompt: request_id=%s, extension_connected=%s, bridge_enabled=%s, tab_id=%s",
                     request_id, self._state.extension_connected, self._state.bridge_enabled, self._state.assigned_tab_id)
        if not self._state.extension_connected:
            logger.warning("Cannot dispatch: extension not connected")
            raise protocol.ErrorCode.CHATGPT_BRIDGE_OFFLINE.value
        if not self._state.bridge_enabled:
            logger.warning("Cannot dispatch: bridge disabled")
            raise protocol.ErrorCode.BRIDGE_DISABLED.value
        if not self._state.assigned_tab_id:
            logger.warning("Cannot dispatch: no tab assigned")
            raise protocol.ErrorCode.NO_CHATGPT_TAB.value

        await self._send(protocol.make_prompt(request_id, prompt, timeout_ms))
        logger.info("Prompt dispatched successfully: request_id=%s", request_id)

    async def start(self) -> None:
        """Start the WebSocket server."""
        await self.connect()
        logger.info("Bridge server started on ws://%s:%d%s", HOST, PORT, WS_PATH)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._state.extension_connected = False
        logger.info("Bridge server stopped")

    async def wait_for_extension_ready(self, timeout: float) -> bool:
        """Wait for the extension to connect.

        Returns True if the extension connected within the timeout,
        False otherwise.
        """
        try:
            await asyncio.wait_for(
                self._extension_ready_event.wait(),
                timeout=timeout,
            )
            logger.info(
                "Extension ready (already connected or connected within %.1fs)",
                timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Extension not ready after %.1fs — first invocation will fail",
                timeout,
            )
            return False
