#!/usr/bin/env python3
"""ChatGPT Bridge — main entry point.

Starts the MCP server (stdio), WebSocket server, and a simple HTTP
server that serves the auth token so the Chrome extension can fetch it
on startup.

Usage:
    python -m server
    or
    python server/main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .config import LOG_PREFIX
from .models import ServerState
from .mcp_server import MCPServer
from .websocket_server import WebSocketServer


def setup_logging() -> None:
    """Configure logging — writes to stderr and to a persistent log file."""
    log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    # Ensure the file exists (so shell redirect doesn't create an empty file)
    if not os.path.exists(log_file):
        open(log_file, "a").close()

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{LOG_PREFIX}] %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file),
        ],
    )


async def start_token_http_server(state: ServerState, port: int = 8766) -> None:
    """Start a minimal HTTP server that serves the auth token.

    GET /token → returns the token as plain text.
    This is safe because the server only binds to 127.0.0.1.
    """
    async def handle_reader(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Read the HTTP request
            data = await asyncio.wait_for(reader.read(1024), timeout=5)
            request = data.decode("utf-8", errors="replace")

            if "GET /token" in request and "HTTP/" in request:
                token = state.ws_token
                body = token.encode("utf-8")
                content_length = f"Content-Length: {len(body)}\r\n"
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain\r\n"
                    + content_length.encode()
                    + b"Access-Control-Allow-Origin: *\r\n"
                    + b"\r\n"
                ) + body
                writer.write(response)
            else:
                response = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
                writer.write(response)

            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_reader, "127.0.0.1", port)
    logger = logging.getLogger("chatgpt-bridge.http")
    logger.info("Token HTTP server on 127.0.0.1:%d", port)
    return server


async def main() -> None:
    """Start the bridge server."""
    try:
        setup_logging()
        logger = logging.getLogger("chatgpt-bridge")

        logger.info("[MAIN] Starting bridge server...")

        state = ServerState()

        ws_server = WebSocketServer(state)
        mcp_server = MCPServer(state, ws_server)

        logger.info("[MAIN] Components initialized")

        # Start WebSocket server
        await ws_server.start()
        logger.info("[MAIN] WebSocket server started")

        # Start token HTTP server (extension fetches token from here)
        token_server = await start_token_http_server(state, port=8766)
        logger.info("[MAIN] Token HTTP server started")

        # Run MCP server (reads from stdin)
        # Use a stop event so we can gracefully shut down on signal
        stop_event = asyncio.Event()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        # Run MCP server and shutdown trigger concurrently.
        # The MCP server runs until stdin closes (EOF).
        # Signals set stop_event, which triggers shutdown.
        # Request timeouts are handled inside the MCP server loop
        # and do NOT propagate here.
        mcp_task = asyncio.create_task(mcp_server.run())
        logger.info("[MAIN] MCP server running, waiting for stdin...")

        try:
            await asyncio.gather(mcp_task, stop_event.wait())
        except asyncio.CancelledError:
            pass
        finally:
            # Gracefully shut down: close WebSocket, then the MCP server
            # will exit when stdin is closed.
            await ws_server.stop()
            token_server.close()
            try:
                await token_server.wait_closed()
            except Exception:
                pass
            # Cancel MCP task if still running (e.g., stdin not closed)
            if not mcp_task.done():
                mcp_task.cancel()
                try:
                    await mcp_task
                except asyncio.CancelledError:
                    pass
            logger.info("Bridge server stopped")
    except Exception as e:
        logger.exception("[MAIN] Fatal error during startup: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
else:
    # When imported as a module, set up logging
    setup_logging()
