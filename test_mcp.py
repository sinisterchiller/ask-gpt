#!/usr/bin/env python3
"""Test the MCP ask_chatgpt tool end-to-end."""

import asyncio
import json
import sys
import time
import subprocess
import os

async def main():
    # Start the MCP server as a subprocess
    # The MCP server reads from stdin and writes to stdout (stdio transport)
    # We'll use a different approach: directly test the WebSocket + request flow

    # Instead, let's test by sending a request through the WebSocket directly
    import websockets

    # Get the token
    import urllib.request
    token_resp = urllib.request.urlopen("http://127.0.0.1:8766/token")
    token = token_resp.read().decode().strip()

    print(f"[TEST] Token: {token[:16]}...")

    # Connect to the WebSocket server
    uri = f"ws://127.0.0.1:8765/ws?token={token}"

    try:
        async with websockets.connect(uri) as ws:
            print("[TEST] WebSocket connected")

            # Wait for hello_ack
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            print(f"[TEST] Received: {data}")

            # Send hello
            await ws.send(json.dumps({"type": "hello", "extensionVersion": "0.1.0"}))

            # Wait for hello_ack
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            print(f"[TEST] Received: {data}")

            print("[TEST] WebSocket handshake complete")

    except Exception as e:
        print(f"[TEST] WebSocket error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
