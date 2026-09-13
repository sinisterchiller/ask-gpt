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
        # We poll stop_event in a loop so asyncio can process the signal
        # handler between iterations — stop_event.wait() alone blocks
        # indefinitely and does not wake when the event is set from a
        # signal handler in all asyncio versions.
        mcp_task = asyncio.create_task(mcp_server.run())
        logger.info("[MAIN] MCP server running, waiting for stdin...")

        try:
            while not stop_event.is_set():
                # Wait for EITHER the stop event OR the MCP task to complete.
                # We create a new task each iteration because stop_event.wait()
                # is a one-shot Future — once resolved, it must be awaited again.
                stop_task = asyncio.create_task(stop_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        [stop_task, mcp_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    stop_task.cancel()
                    break

                # If MCP task completed (stdin EOF), exit loop normally
                if mcp_task in done:
                    break

                # If stop_event was set, exit loop — MCP task will be cancelled
                if stop_task in done:
                    break

                # Cancel any pending tasks
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
        except asyncio.CancelledError:
            pass

        # MCP task is done or cancelled — now shut down remaining services.
        # First cancel the MCP server's background cleanup task.
        if hasattr(mcp_server, '_cleanup_task') and mcp_server._cleanup_task:
            mcp_server._cleanup_task.cancel()
            try:
                await asyncio.wait_for(mcp_server._cleanup_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        # Cancel MCP task if still running
        if not mcp_task.done():
            mcp_task.cancel()
            try:
                await asyncio.wait_for(mcp_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        # If the MCP server used run_in_executor for stdin (pipe stdin),
        # the executor thread may still be blocked on readline().
        # Shut it down with wait=False — the thread will be abandoned,
        # but the process exits immediately after.
        if hasattr(mcp_server, '_uses_add_reader') and not mcp_server._uses_add_reader:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, lambda: None)  # ensure executor exists
            try:
                loop.shutdown_default_executor(wait=False)
            except Exception:
                pass

        # Now close the WebSocket server (MCP task is done/cancelled)
        await ws_server.stop()

        # Close token HTTP server
        token_server.close()
        try:
            await asyncio.wait_for(token_server.wait_closed(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Cancel any remaining tasks to ensure event loop can exit
        all_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in all_tasks:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=0.5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        logger.info("Bridge server stopped")

        # If we shut down the executor above (pipe stdin case), we must
        # exit explicitly — asyncio.run() would hang trying to shut it
        # down again.  For TTY/add-reader stdin the executor is empty
        # and asyncio.run() handles cleanup naturally.
        if hasattr(mcp_server, '_uses_add_reader') and not mcp_server._uses_add_reader:
            sys.exit(0)
    except Exception as e:
        logger.exception("[MAIN] Fatal error during startup: %s", e)
        raise


if __name__ == "__main__":
    asyncio.run(main())
else:
    # When imported as a module, set up logging
    setup_logging()
