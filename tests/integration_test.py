#!/usr/bin/env python3
"""Integration test for the chatgpt-bridge readiness fix.

Tests the full lifecycle by directly using server components (no subprocess).
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets
from server.models import ServerState
from server.websocket_server import WebSocketServer
from server.mcp_server import MCPServer
from server.config import EXTENSION_READY_TIMEOUT

# Colors for terminal output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"


def log(label, msg):
    print(f"{BLUE}[{label}]{RESET} {msg}", flush=True)


def log_ok(label, msg):
    print(f"{GREEN}[{label}]{RESET} {msg}", flush=True)


def log_err(label, msg):
    print(f"{RED}[{label}]{RESET} {msg}", flush=True)


async def run_integration_test():
    """Run the full integration test."""
    log("INTEGRATION", "=== ChatGPT Bridge Readiness Fix Integration Test ===")

    # Create shared state and components
    state = ServerState()
    ws_server = WebSocketServer(state)
    mcp_server = MCPServer(state, ws_server)

    # Start WebSocket server
    await ws_server.start()
    log_ok("INTEGRATION", "WebSocket server started on port 8765")

    # Verify port is listening
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 8765), timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        log_ok("INTEGRATION", "Port 8765 confirmed listening")
    except OSError:
        log_err("INTEGRATION", "Port 8765 not listening")
        return False

    # ---- Test 1: Readiness timeout when extension not connected ----
    print("", flush=True)
    log("TEST 1", "Readiness timeout (extension not connected)...")

    evt_before = ws_server._extension_ready_event.is_set()
    log_ok("TEST 1", f"Ready event before: {evt_before}")
    assert evt_before is False, "Ready event should be unset"
    assert state.extension_connected is False

    start = asyncio.get_event_loop().time()
    ready = await ws_server.wait_for_extension_ready(timeout=EXTENSION_READY_TIMEOUT)
    duration = asyncio.get_event_loop().time() - start

    log("TEST 1", f"Ready after wait: {ready}, duration: {duration:.1f}s")
    assert ready is False, f"Should timeout, got ready={ready}"
    assert 4.0 < duration < 7.0, f"Duration {duration:.1f}s should be ~5s"
    log_ok("TEST 1", f"PASS — timed out after {duration:.1f}s")

    # ---- Test 2: Readiness succeeds when extension connects ----
    print("", flush=True)
    log("TEST 2", "Readiness succeeds after extension connects...")

    token_path = os.path.join(os.path.dirname(__file__), "..", ".bridge_token")
    with open(token_path) as f:
        token = f.read().strip()

    uri = f"ws://127.0.0.1:8765/ws?token={token}"
    ws = await websockets.connect(uri)
    log_ok("INTEGRATION", "Extension WebSocket connected")

    hello_ack = await asyncio.wait_for(ws.recv(), timeout=5.0)
    log("INTEGRATION", f"Received: {hello_ack}")

    assert state.extension_connected is True
    assert ws_server._extension_ready_event.is_set() is True
    log_ok("TEST 2", "PASS — extension connected, ready event set")

    # Readiness should return immediately
    start = asyncio.get_event_loop().time()
    ready = await ws_server.wait_for_extension_ready(timeout=1.0)
    duration = asyncio.get_event_loop().time() - start
    assert ready is True
    assert duration < 0.5
    log_ok("TEST 2", f"PASS — readiness check passed in {duration:.3f}s")

    # ---- Test 3: Assign tab ----
    print("", flush=True)
    log("TEST 3", "Assign tab...")

    await ws.send(json.dumps({
        "type": "tab_changed",
        "tabId": 123,
        "url": "https://chatgpt.com/",
    }))
    await asyncio.sleep(0.1)

    assert state.assigned_tab_id == 123
    log_ok("TEST 3", "PASS — tab assigned")

    # ---- Test 4: Full dispatch + response cycle ----
    print("", flush=True)
    log("TEST 4", "Full prompt dispatch and response cycle...")

    # Create a request in the request manager
    from server.models import BridgeRequest, RequestState
    import time
    request = BridgeRequest(
        request_id="req_test_e2e",
        prompt="bridge-test-ok",
        timeout_ms=10000,
        created_at=time.time(),
    )
    request.future = asyncio.get_event_loop().create_future()
    mcp_server._request_manager._requests["req_test_e2e"] = request
    state.active_request = request

    # Dispatch the prompt
    await ws_server.dispatch_prompt("req_test_e2e", "bridge-test-ok", 10000)
    log_ok("TEST 4", "PASS — prompt dispatched (no ConnectionError)")

    # Simulate extension sending accepted, then response
    await ws.send(json.dumps({"type": "accepted", "requestId": "req_test_e2e"}))
    await asyncio.sleep(0.05)

    await ws.send(json.dumps({
        "type": "response",
        "requestId": "req_test_e2e",
        "response": "bridge-test-ok",
    }))
    await asyncio.sleep(0.05)

    # Wait for the future to be resolved
    result = await asyncio.wait_for(request.future, timeout=2.0)
    assert result == "bridge-test-ok"
    assert request.state == RequestState.COMPLETED
    log_ok("TEST 4", "PASS — full dispatch + response cycle verified")

    # ---- Test 5: Disconnect and ready event lifecycle ----
    print("", flush=True)
    log("TEST 5", "Disconnect and ready event lifecycle...")

    old_event = ws_server._extension_ready_event
    await ws.close()
    await asyncio.sleep(0.3)

    assert state.extension_connected is False
    assert ws_server._extension_ready_event.is_set() is False
    assert ws_server._extension_ready_event is not old_event
    log_ok("TEST 5", "PASS — ready event recreated on disconnect")

    # ---- Test 6: Reconnect and readiness ----
    print("", flush=True)
    log("TEST 6", "Reconnect and readiness...")

    ws2 = await websockets.connect(uri)
    await asyncio.wait_for(ws2.recv(), timeout=5.0)

    assert state.extension_connected is True
    assert ws_server._extension_ready_event.is_set() is True

    ready = await ws_server.wait_for_extension_ready(timeout=0.5)
    assert ready is True
    log_ok("TEST 6", "PASS — reconnect and readiness verified")

    await ws2.close()

    # ---- Summary ----
    print("", flush=True)
    log_ok("INTEGRATION", "=== ALL INTEGRATION TESTS PASSED ===")
    log("INTEGRATION", "Verified:")
    log("INTEGRATION", "  1. Readiness timeout works when extension not connected")
    log("INTEGRATION", "  2. Readiness succeeds immediately when extension is connected")
    log("INTEGRATION", "  3. Tab assignment works via WebSocket protocol")
    log("INTEGRATION", "  4. Full prompt dispatch + response cycle works end-to-end")
    log("INTEGRATION", "  5. Ready event is recreated on disconnect")
    log("INTEGRATION", "  6. Reconnection and readiness cycle works correctly")
    return True


async def main():
    try:
        success = await run_integration_test()
    finally:
        pass
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
