# Ask-GPT Bridge — Handoff Document

## Project Overview

Chrome extension + Python server that lets Claude Code send prompts to ChatGPT via an MCP tool (`ask_chatgpt`). The architecture:

```
Claude Code → MCP (stdio) → Python server → WebSocket → Chrome extension → content.js → ChatGPT page
                                                              ↑                              ↓
                                                              ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←
```

## Architecture

### Files

| File | Role |
|---|---|
| `extension/background.js` | Extension service worker — WebSocket client, request routing, content script management |
| `extension/content.js` | Runs in ChatGPT tab — finds composer, inserts text, clicks send, waits for response |
| `server/main.py` | Entry point — starts WebSocket server, token HTTP server, MCP server |
| `server/mcp_server.py` | MCP stdio server — exposes `ask_chatgpt` tool |
| `server/websocket_server.py` | WebSocket server — routes messages between MCP server and Chrome extension |
| `server/request_manager.py` | Request queue — manages request lifecycle, timeouts, queuing |
| `server/protocol.py` | Message type constants, serialization helpers |
| `server/models.py` | Data models (BridgeRequest, RequestState, ServerState) |
| `server/config.py` | Configuration (ports, timeouts, token) |

### Communication Protocol

**WebSocket messages** (extension ↔ server):
- `hello` / `hello_ack` — connection handshake
- `tab_changed` — extension reports assigned ChatGPT tab
- `prompt` — server sends prompt to extension
- `accepted` — extension acknowledges receipt
- `response` — extension sends ChatGPT response back
- `error` — extension reports failure
- `diagnostic` — debugging telemetry
- `ping` / `pong` — keepalive

**MCP messages** (Claude Code ↔ server):
- `tools/call` with `name: "ask_chatgpt"` — send prompt
- Returns response text or error

## What Was Done This Session

### 10. Reconnect-and-Resend Mechanism (extension/background.js)

**Problem:** `safeWsSend()` could silently fail if the WebSocket connection was broken but `readyState` still reported OPEN. The response would be lost.

**Fix:**
- `sendResponse` now returns the result of `safeWsSend` (boolean)
- If `safeWsSend` returns `false` in `handlePrompt`:
  - Triggers a WebSocket reconnect via `connectWebSocket()`
  - Polls for the WebSocket to become OPEN (up to 10s timeout)
  - Resends the response once reconnected
  - If reconnect times out, sends an error response to the MCP server

### 11. Enhanced `safeWsSend` Diagnostics (extension/background.js)

**Fix:**
- Catches exceptions from `ws.send()` (broken pipe)
- On exception: sets `ws = null`, triggers `scheduleReconnect()`
- Logs detailed diagnostics: type, requestId, ws existence, readyState, wsGeneration

### 12. Diagnostic Logging (server + extension)

Added detailed logging throughout the response path:
- `safeWsSend` (background.js): SENT/FAILED, type, requestId, ws existence, readyState
- `_handle_message` MSG_RESPONSE (websocket_server.py): Response received, request lookup, future state, set_result call
- `_handle_tool_call` (mcp_server.py): Request creation, future creation, dispatch decision, await start, resolution
- `_on_request_complete` (mcp_server.py): Callback invocation, active state, queue size
- `complete_request` (request_manager.py): Method entry, active reference clearing, callback invocation

### 13. Bug Fix: `_on_disconnect` AttributeError (server/websocket_server.py)

**Problem:** `_on_disconnect` accessed `self._state.request_manager` which doesn't exist on `ServerState` by default — it's set by `MCPServer.__init__`. This caused an `AttributeError` when the WebSocket disconnected before the MCP server initialized.

**Fix:** Added `getattr(self._state, "request_manager", None)` guard.

### 1. WebSocket Lifecycle Fix (extension/background.js)

**Problem:** Stale callback race condition. The global `ws` variable was mutated by callbacks from old WebSocket connections, orphaning newer ones.

**Example:**
```
WS#1 exists → new connection initiated → WS#1.close() → WS#2 created
global ws = WS#2 → WS#1 onclose fires late → ws = null → WS#2 orphaned
```

**Fix:**
- Instance-local `socket` variable in `connectWebSocket()` — all callbacks capture this
- `ws !== socket` guard in every callback — stale callbacks exit immediately
- `onerror` no longer touches `ws` — only `onclose` is the authoritative lifecycle transition
- Monotonically increasing `wsGeneration` counter for diagnostics
- `scheduleReconnect()` guards against multiple timers AND against reconnecting when a socket is OPEN/CONNECTING

**Changed files:** `extension/background.js`

### 2. Server Ping/Pong Restoration (server/websocket_server.py)

**Problem:** `ping_interval=None, ping_timeout=None` disabled WebSocket protocol-level keepalive.

**Fix:** Removed those options — `websockets.serve` now uses defaults (`ping_interval=20, ping_timeout=20`). Browser WebSocket implementations handle protocol-level ping/pong transparently in the networking layer.

**Changed files:** `server/websocket_server.py`

### 3. Reconnect Lock During Active Requests (extension/background.js)

**Problem:** Periodic keep-alive (every 15s) could trigger reconnection while a response was in flight, dropping the response.

**Fix:**
- Added `wsReconnectLocked` flag — set to `true` at start of `handlePrompt()`, set to `false` after response/error
- Keep-alive timer respects the lock — won't reconnect while locked
- Reconnection only happens via `onclose`/`scheduleReconnect`

**Changed files:** `extension/background.js`

### 4. Content Script Response Extraction Fix (extension/content.js)

**Problem:** The response extraction was capturing the "Thinking" element (8 chars) instead of the actual response. ChatGPT renders "Thinking" and the actual response in separate DOM elements.

**Fix:**
- `startObservation()` interval timer re-queries assistant messages every second
- Walks messages backwards from the end, skipping "Thinking" elements (length ≤ 10)
- Picks the first real message (length > 10)
- 2-second minimum observation before stabilization (prevents premature capture during "Thinking" phase)
- 3 consecutive stable checks before resolving (text must not change for 3 seconds)

**Changed files:** `extension/content.js`

### 5. Message Element Tracking Fix (extension/content.js)

**Problem:** `messageEl` was stuck on the old "Thinking" element across multiple prompts in the same chat.

**Fix:**
- `startObservation()` walks assistant messages backwards from the end
- Skips "Thinking" elements (length ≤ 10)
- Picks the first real message (length > 10)
- Tracks message count — when count increases, resets to pick the new message

**Changed files:** `extension/content.js`

### 6. Duplicate Submission Prevention (extension/content.js)

**Problem:** Re-injecting content.js added duplicate listeners. Two listeners fired simultaneously, both seeing `state === "IDLE"`, both proceeding with submissions.

**Fix:**
- `globalThis.__chatgptBridgeProcessingLock` — survives re-injection (stored on globalThis)
- `globalThis.__chatgptBridgeListenerRegistered` — prevents duplicate listener registration
- Synchronous lock check — even if two listeners fire in the same tick, only one proceeds

**Changed files:** `extension/content.js`

### 7. Module-Level Variable Scoping (extension/content.js)

**Problem:** `waitForResponse()` and `startObservation()` were separate functions with different scopes. Variables like `messageEl`, `lastText`, `stableCount`, `requestId` were inaccessible between them.

**Fix:**
- Moved shared variables to module level: `messageEl`, `stableCount`, `lastText`, `lastRequestId`, `responseResolve`, `responseTimeoutHandle`, `generationStartAt`
- `waitForResponse()` sets module-level state before calling `startObservation()`
- `startObservation()` reads module-level state

**Changed files:** `extension/content.js`

### 8. MCP Dispatch Bug Fix (server/mcp_server.py)

**Problem:** The `create_request()` method auto-activates the request, so `activate_next()` returned `None`. The request was never dispatched to the extension.

**Fix:**
- Check if the request is already active (auto-activated by `create_request`)
- If active, dispatch immediately
- If queued, wait for the active request to finish (handled by `_on_request_complete`)

**Changed files:** `server/mcp_server.py`

### 9. MCP Server Startup Fix (server/__main__.py, .claude/settings.json)

**Problem:** MCP server was crashing on startup with no visible error.

**Fix:**
- Added try/except in `__main__.py` to catch and log startup errors
- Changed MCP config to use `run_mcp.sh` wrapper script for better error visibility

**Changed files:** `server/__main__.py`, `.claude/settings.json`, `run_mcp.sh`

## Current State

### ✅ Working

| Component | Status | Evidence |
|---|---|---|
| Extension → ChatGTS tab injection | PASS | `chrome.scripting.executeScript` works |
| Content script message handling | PASS | `check_tab` returns `{isChatGPT: true, ready: true}` |
| Prompt submission to ChatGPT | PASS | Prompt appears in composer and is submitted |
| Response capture | PASS | Full response (292+ chars) captured, not just "Thinking" |
| chrome.tabs.sendMessage resolution | PASS | Returns `{success: true, response: "..."}` |
| WebSocket connection | PASS | Extension connects to server, receives pong |
| WebSocket generation IDs | PASS | `[WS#1]`, `[WS#2]` prefixed on all logs |
| Reconnect lock | PASS | Won't reconnect during active requests |
| Server ping/pong | RESTORED | Default ping_interval=20, ping_timeout=20 |

### ❌ Still Broken

| Component | Problem | Evidence |
|---|---|---|
| **MCP → Response transport** | **Response captured from ChatGPT but never reaches MCP server** | `CHATGPT_TIMEOUT last_stage=content_response_received elapsed=300.0s` — content script says `success=true` but MCP server waits 5 minutes |

### 🔍 Latest Fix: Reconnect-and-Resend (background.js)

Added a **reconnect-and-resend** mechanism in `handlePrompt`:

1. `sendResponse` now returns the result of `safeWsSend` (boolean)
2. If `safeWsSend` returns `false` (connection broken):
   - Triggers a WebSocket reconnect via `connectWebSocket()`
   - Polls for the WebSocket to become OPEN (up to 10s timeout)
   - Resends the response once reconnected
   - If reconnect times out, sends an error response to the MCP server

### Enhanced `safeWsSend`

- Catches exceptions from `ws.send()` (broken pipe)
- On exception: sets `ws = null`, triggers `scheduleReconnect()`
- Logs detailed diagnostics: type, requestId, ws existence, readyState, wsGeneration

### Diagnostic Logging Added

| Location | What's Logged |
|---|---|
| `safeWsSend` (background.js) | SENT/FAILED, type, requestId, ws existence, readyState, wsGeneration |
| `_handle_message` MSG_RESPONSE (websocket_server.py) | Response received, request lookup, future state, set_result call |
| `_handle_tool_call` (mcp_server.py) | Request creation, future creation, dispatch decision, await start, resolution |
| `_on_request_complete` (mcp_server.py) | Callback invocation, active state, queue size |
| `complete_request` (request_manager.py) | Method entry, active reference clearing, callback invocation |

### Remaining Hypotheses

1. **WebSocket connection broken but `readyState` still OPEN** — `ws.send()` doesn't throw, but data is lost. The reconnect-and-resend won't catch this since `safeWsSend` returns true. May need TCP-level keepalive or application-level ping/pong verification.

2. **Server not receiving the message** — The WebSocket message is sent but never reaches the server's async handler. Could be a network issue or the server's message loop is blocked.

3. **Server receives but can't route** — The server receives the message but the request lookup fails (wrong request ID, future already done). The diagnostic logging will confirm this.

**Next debugging step:** Reload the extension in Chrome, start the server, run an MCP test, and check server logs for the diagnostic output.

## Server Status

- **WebSocket server:** `ws://127.0.0.1:8765/ws`
- **Token HTTP server:** `http://127.0.0.1:8766/token`
- **MCP server:** stdio transport (managed by Claude Code)
- **Token:** `407ee051fabdc17f024c7b622e5f7e890fe1fd85935dd92883eaed82a6ea743f`

## How to Test

### 1. Direct Content Script Test (proven working)

```js
// In extension service worker console:
chrome.scripting.executeScript({
  target: { tabId: <TAB_ID>, allFrames: false },
  func: () => { delete globalThis.__chatgptBridgeInitialized; delete globalThis.__chatgptBridgeListenerRegistered; delete globalThis.__chatgptBridgeProcessingLock; }
}).then(() =>
  chrome.scripting.executeScript({
    target: { tabId: <TAB_ID>, allFrames: false },
    files: ["content.js"]
  }).then(() => {
    setTimeout(() => {
      chrome.tabs.sendMessage(<TAB_ID>, {
        action: "submit_prompt",
        prompt: "TEST_UNIQUE_PROMPT — please write exactly one sentence about the history of computing.",
        requestId: "direct_test"
      }).then(
        r => console.log("RESULT:", JSON.stringify(r)),
        e => console.error("FAILED:", e.message)
      )
    }, 600)
  })
)
```

### 2. MCP Test

```
claude mcp list  # Should show chatgpt-bridge connected
ask_chatgpt "test prompt"
```

## Known Issues to Address

1. **MCP response transport** — The most critical remaining issue. The response is captured but never reaches the MCP server.
2. **Extension needs reload** — The extension's JavaScript hasn't been reloaded in the browser to pick up the latest fixes. Need to reload the extension in Chrome.
3. **ChatGPT tab ID** — Tab ID 1139884058 was used for testing. May need to be updated if the tab is closed.

## Files Modified This Session

- `extension/background.js` — WebSocket lifecycle, reconnect lock, generation IDs
- `extension/content.js` — Response extraction, duplicate prevention, scoping fixes
- `server/mcp_server.py` — Dispatch bug fix
- `server/websocket_server.py` — Ping/pong restoration, response validation
- `server/__main__.py` — Error handling
- `.claude/settings.json` — MCP config
- `run_mcp.sh` — Wrapper script

## Files NOT Modified

- `server/main.py` — No changes
- `server/request_manager.py` — No changes
- `server/protocol.py` — No changes
- `server/models.py` — No changes
- `server/config.py` — No changes
- `extension/content.js` (selectors) — No changes to `SELECTORS` object
- `extension/manifest.json` — No changes
