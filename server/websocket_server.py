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
        # Event set when extension's initial sync completes (tab_changed received).
        # This is different from _ws_connected — the extension must send
        # tab_changed after connecting so the server knows the assigned tab.
        self._extension_ready_event: asyncio.Event = asyncio.Event()
        # Track whether the current connection has completed initial sync
        self._sync_complete = False

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
            # Default ping_interval=20, ping_timeout=20 — browser handles
            # protocol-level ping/pong transparently; application-level
            # pings ({"type":"ping"}) are a separate keepalive layer.
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
        logger.info("[WS] extension connection attempt")

        self._ws = ws
        self._state.extension_connected = True
        self._disconnected_event.clear()
        self._sync_complete = False
        # Do NOT set ready event here — wait for tab_changed.
        # This prevents NO_CHATGPT_TAB when the extension connects
        # but tab_changed hasn't arrived yet.
        self._extension_ready_event = asyncio.Event()
        logger.info("[WS] extension connected (waiting for state sync)")

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
        # Log raw message type for transport diagnostics
        try:
            preview = raw[:200] if len(raw) > 200 else raw
            logger.debug("[WS RAW] %s", preview)
        except Exception:
            pass
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

            # On first tab_changed after connection, signal that the
            # extension is fully ready (connected + synced state).
            if not self._sync_complete:
                self._sync_complete = True
                self._extension_ready_event.set()
                logger.info("[WS] extension synced (tab=%s)", tab_id)

        elif msg_type == protocol.MSG_ACCEPTED:
            request_id = msg.get("requestId")
            if request_id:
                logger.info("Request %s accepted by extension", request_id)

        elif msg_type == protocol.MSG_RESPONSE:
            if not protocol.validate_response(msg):
                logger.warning("Invalid response message: %s", json.dumps(msg) if isinstance(msg, dict) else msg)
                return
            request_id = msg["requestId"]
            response_text = msg["response"]
            url = msg.get("url", "")

            logger.info("[WS RX] type=response requestId=%s response_len=%d",
                        request_id, len(response_text))
            logger.info("[REQ %s] SERVER_RESPONSE_RECEIVED", request_id)
            logger.info("[WS] Received RESPONSE for request %s (%d chars), ws=%s, ws_state=%s",
                        request_id, len(response_text), self._ws is not None,
                        self._ws.state.value if self._ws else "N/A")

            # Only process responses from the current active WebSocket connection.
            # This prevents stale responses from being processed after a reconnect.
            # The server's async loop only processes messages on the current connection,
            # so this is a safety check.
            if self._ws is None or self._ws.state.value == 3:
                logger.warning("Response received but WebSocket is not connected")
                return

            req = self._state.request_manager.get_request(request_id)
            logger.info("[WS] get_request(%s) -> found=%s, future=%s, future.done=%s",
                        request_id, req is not None,
                        req.future if req else "N/A",
                        req.future.done() if (req and req.future) else "N/A")
            if req and req.future and not req.future.done():
                logger.info("[REQ %s] PENDING_FUTURE_FOUND setting result", request_id)
                req.response = response_text
                req.state = RequestState.COMPLETED
                req.future.set_result(response_text)
                logger.info("[WS] future.set_result called for %s", request_id)
                self._state.request_manager.complete_request(request_id, response=response_text)
                logger.info("[REQ %s] FUTURE_RESOLVED (%d chars)", request_id, len(response_text))
            else:
                logger.warning("Response for unknown request or future already done: %s (req=%s, future=%s, future_done=%s)",
                              request_id, req is not None,
                              req.future if req else "N/A",
                              req.future.done() if (req and req.future) else "N/A")

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

        elif msg_type == protocol.MSG_DIAGNOSTIC:
            request_id = msg.get("requestId", "")
            component = msg.get("component", "")
            stage = msg.get("stage", "")
            message = msg.get("message", "")
            logger.info(
                "[REQ %s] DIAGNOSTIC component=%s stage=%s message=%s",
                request_id, component, stage, message,
            )
            # Update the last_stage for the active request
            req = self._state.request_manager.get_request(request_id)
            if req:
                req.last_stage = stage
                req.last_stage_time = time.time()

        else:
            logger.warning("Unknown message type: %s", msg_type)

    async def _on_disconnect(self) -> None:
        """Handle WebSocket disconnection."""
        self._state.extension_connected = False
        self._ws = None
        self._disconnected_event.set()
        self._sync_complete = False
        # Create a fresh event so the next tool call waits for reconnection
        self._extension_ready_event = asyncio.Event()
        logger.info("[WS] extension disconnected — cleaning up active request")

        # request_manager may not be set if called before MCPServer init
        rm = getattr(self._state, "request_manager", None)
        if rm:
            active = rm.active
            if active:
                rm.fail_request(
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
        logger.info("[REQ %s] dispatched", request_id)

    async def start(self) -> None:
        """Start the WebSocket server."""
        await self.connect()
        logger.info("[WS] listener ready on ws://%s:%d%s", HOST, PORT, WS_PATH)

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._state.extension_connected = False
        logger.info("[WS] server stopped")

    async def wait_for_extension_ready(self, timeout: float) -> bool:
        """Wait for the extension to connect AND sync its state.

        The extension is considered ready only when:
        1. The WebSocket is connected, AND
        2. The extension has sent tab_changed (state synced)

        Returns True if both conditions are met within the timeout,
        False otherwise.
        """
        try:
            await asyncio.wait_for(
                self._extension_ready_event.wait(),
                timeout=timeout,
            )
            logger.info(
                "Extension ready (connected and synced within %.1fs)",
                timeout,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                "Extension not ready after %.1fs (connected=%s, synced=%s)",
                timeout,
                self._state.extension_connected,
                self._sync_complete,
            )
            return False
