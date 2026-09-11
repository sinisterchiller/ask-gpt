/**
 * ChatGPT Bridge — Background service worker (Manifest V3).
 *
 * Responsibilities:
 * - WebSocket connection to localhost bridge server
 * - Reconnection with backoff
 * - Tab assignment management (persisted)
 * - Content-script injection and health checks
 * - Request routing to content script
 * - Response collection and forwarding
 */

"use strict";

// ------------------------------------------------------------------
// Configuration
// ------------------------------------------------------------------

const SERVER_URL = "ws://127.0.0.1:8765/ws";
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const RECONNECT_FACTOR = 2;

// ------------------------------------------------------------------
// State
// ------------------------------------------------------------------

let ws = null;
let wsGeneration = 0; // Monotonically increasing generation ID for each WebSocket instance
let wsToken = "";
let assignedTabId = null;
let bridgeEnabled = true;
let reconnectAttempts = 0;
let reconnectTimer = null;
let lastPongAt = 0; // Last pong timestamp for heartbeat health check
let wsReconnectLocked = false; // Prevent reconnect during active requests

// ------------------------------------------------------------------
// Service Worker persistence
// ------------------------------------------------------------------

async function loadState() {
  try {
    var stored = await chrome.storage.local.get([
      "assignedTabId",
      "bridgeEnabled",
      "wsToken",
    ]);
    if (stored.assignedTabId !== undefined) assignedTabId = stored.assignedTabId;
    if (stored.bridgeEnabled !== undefined) bridgeEnabled = stored.bridgeEnabled;
    if (stored.wsToken !== undefined) wsToken = stored.wsToken;
    console.log("[chatgpt-bridge] State loaded:", {
      assignedTabId,
      bridgeEnabled,
      wsToken: wsToken ? "set" : "unset",
    });
  } catch (err) {
    console.error("[chatgpt-bridge] Failed to load state:", err);
  }
}

async function saveState() {
  try {
    await chrome.storage.local.set({
      assignedTabId: assignedTabId,
      bridgeEnabled: bridgeEnabled,
      wsToken: wsToken,
    });
  } catch (err) {
    console.error("[chatgpt-bridge] Failed to save state:", err);
  }
}

// ------------------------------------------------------------------
// WebSocket connection
// ------------------------------------------------------------------

/**
 * Safe WebSocket send — single entry point that guards against null,
 * closed connections, and stale generations.  Every send goes through
 * here so there is exactly one place to check ws.readyState.
 *
 * If the connection appears open but send fails (broken pipe), we
 * trigger an immediate reconnect so the next message has a chance
 * to go through a fresh socket.
 *
 * @param {object} message
 * @returns {boolean} true if sent, false if connection unavailable
 */
function safeWsSend(message) {
  // Fast path: connection is confirmed open
  if (ws && ws.readyState === WebSocket.OPEN) {
    try {
      ws.send(JSON.stringify(message));
      console.log("[chatgpt-bridge][safeWsSend] SENT type=" + (message.type || "") + " req=" + (message.requestId || "") + " wsGen=" + wsGeneration);
      return true;
    } catch (err) {
      // Send threw — connection is broken even though readyState said OPEN
      console.error("[chatgpt-bridge][safeWsSend] send threw:", err.message, "wsGen=" + wsGeneration, "readyState=" + ws.readyState);
      // Trigger reconnect — the old socket is dead
      ws = null;
      scheduleReconnect();
      return false;
    }
  }

  // Connection is not open — log and return false
  console.warn("[chatgpt-bridge][safeWsSend] FAILED type=" + (message.type || "") + " req=" + (message.requestId || "") +
    " ws=" + (ws ? "exists" : "null") +
    " readyState=" + (ws !== null ? ws.readyState : "N/A") +
    " wsGen=" + wsGeneration);
  return false;
}


/**
 * Connect a new WebSocket with strict lifecycle isolation.
 *
 * Each connection gets its own local `socket` variable.
 * All callbacks capture this variable and check
 * `ws === socket` before acting, so stale callbacks from
 * a previous connection can never mutate the state of a
 * newer one.
 *
 * Generation IDs are logged for diagnostics.
 */
function connectWebSocket() {
  // --- Guard: don't create a new socket if one is already OPEN or CONNECTING ---
  if (ws) {
    if (
      ws.readyState === WebSocket.OPEN ||
      ws.readyState === WebSocket.CONNECTING
    ) {
      console.log("[chatgpt-bridge] Socket already " +
        (ws.readyState === WebSocket.OPEN ? "open" : "connecting") +
        " — skipping new connection");
      return;
    }
    // Socket exists but is in CLOSING/CLOSED state — close it explicitly
    // so the old socket's onclose fires before we create the new one.
    try { ws.close(); } catch (e) { /* ignore */ }
    ws = null;
  }

  // Increment generation — every new socket gets a unique ID
  var generation = ++wsGeneration;

  var url = SERVER_URL + (wsToken ? "?token=" + wsToken : "");
  console.log(
    "[chatgpt-bridge][WS#" + generation + "] Connecting to " +
    url.replace(/token=[^&]*/, "token=***")
  );

  var socket;
  try {
    socket = new WebSocket(url);
  } catch (err) {
    console.error("[chatgpt-bridge][WS#" + generation + "] WebSocket creation failed:", err);
    scheduleReconnect();
    return;
  }

  // Install this socket as the global BEFORE assigning handlers,
  // so safeWsSend can see it immediately.
  ws = socket;

  // ---- onopen ----
  socket.onopen = function () {
    if (ws !== socket) {
      console.log("[chatgpt-bridge][WS#" + generation + "] late open ignored (generation " + wsGeneration + ")");
      socket.close();
      return;
    }
    console.log("[chatgpt-bridge][WS#" + generation + "] open");
    reconnectAttempts = 0;

    socket.send(JSON.stringify({
      type: "hello",
      extensionVersion: "0.1.0",
    }));

    console.log("[chatgpt-bridge][WS#" + generation + "] STATE bridgeEnabled=" +
      bridgeEnabled + " assignedTabId=" + assignedTabId);
    socket.send(JSON.stringify({
      type: "tab_changed",
      tabId: assignedTabId,
      url: "",
    }));
    console.log("[chatgpt-bridge][WS#" + generation + "] sync_state sent");
  };

  // ---- onmessage ----
  socket.onmessage = function (event) {
    if (ws !== socket) return;
    handleMessage(event.data);
  };

  // ---- onerror — diagnostics only, DO NOT clear ws or schedule reconnect ----
  socket.onerror = function (err) {
    if (ws !== socket) {
      console.log("[chatgpt-bridge][WS#" + generation + "] late error ignored (generation " + wsGeneration + ")");
      return;
    }
    console.error(
      "[chatgpt-bridge][WS#" + generation + "] error — readyState=" + socket.readyState
    );
    // Do NOT set ws = null here. onclose is the authoritative
    // lifecycle transition.  onerror does not guarantee the
    // connection is closed.
  };

  // ---- onclose — authoritative lifecycle transition ----
  socket.onclose = function (event) {
    if (ws !== socket) {
      console.log(
        "[chatgpt-bridge][WS#" + generation + "] late close ignored " +
        "(code=" + event.code + ", reason=\"" + event.reason + "\", clean=" + event.wasClean + ") " +
        "generation=" + wsGeneration
      );
      return;
    }
    console.log(
      "[chatgpt-bridge][WS#" + generation + "] closed " +
      "(code=" + event.code + ", reason=\"" + event.reason + "\", clean=" + event.wasClean + ")"
    );
    ws = null;
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  // Guard: never have more than one reconnect timer running
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  // Guard: never start a reconnect if an OPEN or CONNECTING socket exists
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    console.log("[chatgpt-bridge] Reconnect skipped — socket already " +
      (ws.readyState === WebSocket.OPEN ? "open" : "connecting"));
    return;
  }

  var delay = Math.min(
    RECONNECT_BASE_MS * Math.pow(RECONNECT_FACTOR, reconnectAttempts),
    RECONNECT_MAX_MS
  );
  reconnectAttempts++;

  console.log("[chatgpt-bridge] Reconnecting in " + delay + "ms (attempt " + (reconnectAttempts + 1) + ")");
  reconnectTimer = setTimeout(function () {
    reconnectTimer = null;
    connectWebSocket();
  }, delay);
}

/**
 * @deprecated Use safeWsSend() instead.
 * Kept for temporary backward compatibility.
 */
function sendMessage(msg) {
  return safeWsSend(msg);
}

// ------------------------------------------------------------------
// Message handling
// ------------------------------------------------------------------

function handleMessage(raw) {
  var msg;
  try {
    msg = JSON.parse(raw);
  } catch (e) {
    console.error("[chatgpt-bridge] Invalid JSON:", raw);
    return;
  }

  console.log("[chatgpt-bridge] Received:", msg.type, msg.requestId || "");

  switch (msg.type) {
    case "hello_ack":
      console.log("[chatgpt-bridge] Server acknowledged, protocol version:", msg.protocolVersion);
      break;

    case "pong":
      // Heartbeat response from the server — update health timestamp.
      // Distinguised from application-level content-script pings.
      lastPongAt = Date.now();
      break;

    case "prompt":
      handlePrompt(msg);
      break;

    case "bridge_state":
      if (msg.enabled !== undefined) {
        bridgeEnabled = msg.enabled;
        saveState();
      }
      break;

    default:
      console.warn("[chatgpt-bridge] Unknown message type:", msg.type);
  }
}

// ------------------------------------------------------------------
// Content-script health check and auto-injection
// ------------------------------------------------------------------

/**
 * Send a message to a content script with a real timeout.
 * Chrome's sendMessage timeout option is unreliable in MV3.
 * @param {number} tabId
 * @param {object} message
 * @param {number} timeoutMs
 * @returns {Promise<*>}
 */
function sendMessageToContent(tabId, message, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var timedOut = false;
    var timer = setTimeout(function () {
      timedOut = true;
      reject(new Error("Content script did not respond within " + timeoutMs + "ms"));
    }, timeoutMs);

    chrome.tabs.sendMessage(tabId, message, function (response) {
      if (timedOut) return;
      clearTimeout(timer);

      var lastError = chrome.runtime.lastError;
      if (lastError) {
        reject(new Error(lastError.message));
        return;
      }
      resolve(response);
    });
  });
}

/**
 * Check if the content script is loaded and responsive in the given tab.
 * @param {number} tabId
 * @returns {Promise<boolean>}
 */
function pingContentScript(tabId) {
  return sendMessageToContent(tabId, { action: "check_tab" }, 2000)
    .then(function (result) {
      if (result && result.isChatGPT !== undefined) {
        return true;
      }
      return false;
    })
    .catch(function (err) {
      console.error("[content-script] ping failed: " + err.message);
      return false;
    });
}

/**
 * Ensure the content script is loaded and responsive in the given tab.
 *
 * Flow:
 *   1. Ping the content script.
 *   2. If pong → done.
 *   3. If no receiver → inject content.js via chrome.scripting.executeScript.
 *   4. Wait for initialization, then ping again.
 *   5. If still unavailable → throw explicit error.
 *
 * @param {number} tabId
 * @returns {Promise<void>} Resolves when the content script is confirmed ready.
 */
async function ensureContentScript(tabId) {
  // Step 1: Try ping first (fast path — script already loaded).
  try {
    var alive = await pingContentScript(tabId);
    if (alive) {
      console.log("[content-script] ping OK (pre-existing)");
      return;
    }
  } catch (err) {
    // Receiving end doesn't exist or other error — proceed to inject.
    console.log("[content-script] ping failed, will inject: " + err.message);
  }

  // Step 2: Verify tab still exists and is a ChatGPT page.
  var tab;
  try {
    tab = await chrome.tabs.get(tabId);
    if (!tab || !tab.url || !tab.url.includes("chatgpt.com")) {
      throw new Error("Tab is not a ChatGPT page");
    }
  } catch (err) {
    console.error("[content-script] tab validation failed: " + err.message);
    throw new Error(
      "CONTENT_SCRIPT_UNAVAILABLE: Tab " + tabId + " is not a valid ChatGPT page. " + err.message
    );
  }

  // Step 3: Inject content.js.
  try {
    console.log("[content-script] injecting content.js into tab " + tabId);
    await chrome.scripting.executeScript({
      target: { tabId: tabId, allFrames: false },
      files: ["content.js"],
    });
  } catch (err) {
    console.error("[content-script] injection failed: " + err.message);
    throw new Error(
      "CONTENT_SCRIPT_UNAVAILABLE: Failed to inject content.js into tab " + tabId + ". " + err.message
    );
  }

  // Step 4: Wait for the injected script to initialize.
  await new Promise(function (r) { setTimeout(r, 500); });

  // Step 5: Re-ping to confirm.
  var postInjectAlive = await pingContentScript(tabId);
  if (!postInjectAlive) {
    throw new Error(
      "CONTENT_SCRIPT_UNAVAILABLE: No content-script receiver was available in assigned ChatGPT tab " +
      tabId + ". Automatic reinjection failed."
    );
  }

  console.log("[content-script] content script available (via auto-inject)");
}

// ------------------------------------------------------------------
// Diagnostic forwarding
// ------------------------------------------------------------------

/**
 * Send a diagnostic message to the Python server.
 * @param {string} requestId
 * @param {string} component
 * @param {string} stage
 * @param {string} [message]
 */
function sendDiagnostic(requestId, component, stage, message) {
  safeWsSend({
    type: "diagnostic",
    requestId: requestId,
    component: component,
    stage: stage,
    message: message || "",
  });
  console.log("[req_" + requestId + "] DIAGNOSTIC " + component + "/" + stage + (message ? ": " + message : ""));
}

// ------------------------------------------------------------------
// Prompt handling
// ------------------------------------------------------------------

async function handlePrompt(msg) {
  var requestId = msg.requestId;
  var prompt = msg.prompt;
  var timeout = msg.timeout || 300000;

  // Lock reconnection while processing this request to prevent
  // the keep-alive timer from closing the WebSocket mid-response.
  wsReconnectLocked = true;

  console.log("[req_" + requestId + "] request received from server");
  sendDiagnostic(requestId, "background", "request_received");

  // Validate bridge state
  if (!bridgeEnabled) {
    console.log("[req_" + requestId + "] bridge disabled");
    sendDiagnostic(requestId, "background", "bridge_disabled");
    sendResponse(requestId, null, {
      success: false,
      error: "Bridge is disabled",
      code: "BRIDGE_DISABLED",
    });
    return;
  }

  if (!assignedTabId) {
    console.log("[req_" + requestId + "] no assigned tab");
    sendDiagnostic(requestId, "background", "no_assigned_tab");
    sendResponse(requestId, null, {
      success: false,
      error: "No ChatGPT tab assigned",
      code: "NO_CHATGPT_TAB",
    });
    return;
  }

  console.log("[req_" + requestId + "] assigned tab=" + assignedTabId);
  sendDiagnostic(requestId, "background", "tab_validated", "id=" + assignedTabId);

  // Send accepted back to server
  safeWsSend({
    type: "accepted",
    requestId: requestId,
  });
  sendDiagnostic(requestId, "background", "accepted_sent");

  // Validate tab exists
  var tab;
  try {
    tab = await chrome.tabs.get(assignedTabId);
    if (!tab) {
      throw new Error("Tab not found");
    }
  } catch (err) {
    console.log("[req_" + requestId + "] tab not found, clearing assignment");
    sendDiagnostic(requestId, "background", "tab_not_found", err.message);
    sendResponse(requestId, null, {
      success: false,
      error: "Assigned tab no longer exists",
      code: "ASSIGNED_TAB_CLOSED",
    });
    assignedTabId = null;
    saveState();
    return;
  }

  // Check if tab is a ChatGPT page
  if (!tab.url || !tab.url.includes("chatgpt.com")) {
    console.log("[req_" + requestId + "] tab is not ChatGPT: " + tab.url);
    sendDiagnostic(requestId, "background", "tab_not_chatgpt", tab.url);
    sendResponse(requestId, null, {
      success: false,
      error: "Assigned tab is not a ChatGPT page",
      code: "CHATGPT_NOT_READY",
    });
    return;
  }
  sendDiagnostic(requestId, "background", "tab_is_chatgpt", tab.url);

  // Ensure content script is loaded and responsive.
  // This handles both:
  //   - Cold start: content script never loaded (extension reload after tab open)
  //   - Warm path: content script already running
  try {
    await ensureContentScript(assignedTabId);
  } catch (err) {
    console.error("[req_" + requestId + "] content script unavailable: " + err.message);
    sendDiagnostic(requestId, "background", "content_script_unavailable", err.message);
    sendResponse(requestId, null, {
      success: false,
      error: err.message,
      code: "CONTENT_SCRIPT_UNAVAILABLE",
    });
    return;
  }
  sendDiagnostic(requestId, "background", "content_script_ready", "ensured");

  // Send prompt to content script with a real timeout.
  try {
    console.log("[req_" + requestId + "] sending to content script");
    sendDiagnostic(requestId, "background", "dispatching_to_content", "action=submit_prompt");
    var result = await sendMessageToContent(assignedTabId, {
      action: "submit_prompt",
      prompt: prompt,
      requestId: requestId,
    }, timeout);

    console.log("[req_" + requestId + "] content script response:", result);
    sendDiagnostic(requestId, "background", "content_response_received",
      "success=" + (result ? result.success : "null") + " response_len=" + (result && result.response ? result.response.length : 0));

    // Send response to server. If safeWsSend fails (connection broken),
    // attempt a reconnect and retry once. This handles the case where
    // the WebSocket connection drops right after the content script
    // captures the response but before we forward it to the server.

    // --- DIAGNOSTIC: log exact outbound response payload ---
    console.log(
      "[req_" + requestId + "] RESPONSE_FORWARD_START " +
      "wsExists=" + (ws !== null) +
      " readyState=" + (ws ? ws.readyState : "N/A") +
      " msgType=response" +
      " responseLen=" + (result.response || "").length
    );

    var responseSent;
    try {
      responseSent = sendResponse(requestId, result, {
        success: result.success,
        response: result.response || "",
        url: tab.url,
      });
      console.log(
        "[req_" + requestId + "] sendResponse() returned " + responseSent
      );
    } catch (sendErr) {
      console.error(
        "[req_" + requestId + "] sendResponse() THREW: " + sendErr.message +
        " stack=" + sendErr.stack
      );
      responseSent = false;
    }

    if (responseSent) {
      console.log(
        "[req_" + requestId + "] RESPONSE_FORWARD_SENT requestId=" + requestId
      );
    } else {
      console.error(
        "[req_" + requestId + "] RESPONSE_FORWARD_FAILED " +
        "wsExists=" + (ws !== null) +
        " readyState=" + (ws ? ws.readyState : "N/A") +
        " wsGen=" + wsGeneration
      );
    }

    if (!responseSent) {
      // Connection appears broken — try to reconnect and resend.
      console.log("[req_" + requestId + "] safeWsSend failed, attempting reconnect + resend");
      try {
        await new Promise(function (resolve, reject) {
          var reconnectStarted = false;
          var origOnOpen = null;

          // Wait for the WebSocket to become open
          function waitForOpen() {
            if (ws && ws.readyState === WebSocket.OPEN) {
              resolve();
            } else if (ws && ws.readyState === WebSocket.CONNECTING) {
              // Connection in progress — wait for onopen
              if (!reconnectStarted) {
                reconnectStarted = true;
                console.log("[req_" + requestId + "] Waiting for reconnect...");
              }
              setTimeout(waitForOpen, 100);
            } else {
              // Not connected — trigger reconnect
              if (!reconnectStarted) {
                reconnectStarted = true;
                console.log("[req_" + requestId + "] Triggering reconnect...");
                connectWebSocket();
              }
              setTimeout(waitForOpen, 100);
            }
          }
          waitForOpen();

          // Timeout after 10 seconds
          setTimeout(function () {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
              reject(new Error("Reconnect timed out after 10s"));
            } else {
              resolve();
            }
          }, 10000);
        }).then(function () {
          // Reconnected — try sending again
          var resendSent = sendResponse(requestId, result, {
            success: result.success,
            response: result.response || "",
            url: tab.url,
          });
          if (!resendSent) {
            console.error("[req_" + requestId + "] Resend also failed after reconnect");
          } else {
            console.log("[req_" + requestId + "] Resend succeeded after reconnect");
          }
        }).catch(function (err) {
          console.error("[req_" + requestId + "] Reconnect failed:", err.message);
          // Send error response to server so it doesn't hang
          sendResponse(requestId, null, {
            success: false,
            error: "WebSocket connection lost during response: " + err.message,
            code: "CONNECTION_LOST",
          });
        });
      } catch (err) {
        console.error("[req_" + requestId + "] Reconnect error:", err.message);
        sendResponse(requestId, null, {
          success: false,
          error: "WebSocket connection lost during response: " + err.message,
          code: "CONNECTION_LOST",
        });
      }
    }

  } catch (err) {
    console.error("[chatgpt-bridge] Content script error:", err.message);
    sendDiagnostic(requestId, "background", "content_script_error", err.message);
    sendResponse(requestId, null, {
      success: false,
      error: "Content script error: " + err.message,
      code: "CHATGPT_COMPOSER_NOT_FOUND",
    });
  }

  // Unlock reconnection — the request is complete (success or error).
  wsReconnectLocked = false;
}

function sendResponse(requestId, result, serverMsg) {
  var message;
  if (result && result.success) {
    // Caller may pass a partial object with { success, response, url }
    // — always construct the full protocol message.
    message = {
      type: "response",
      requestId: requestId,
      response: (serverMsg && serverMsg.response) || result.response || "",
      url: (serverMsg && serverMsg.url) || "",
    };
  } else {
    message = {
      type: "error",
      requestId: requestId,
      code: (serverMsg && serverMsg.code) || (result ? result.code || "UNKNOWN_ERROR" : "UNKNOWN_ERROR"),
      message: (serverMsg && serverMsg.message) || (result ? result.error || "Unknown error" : "Unknown error"),
    };
  }
  return safeWsSend(message);
}

// ------------------------------------------------------------------
// Tab management
// ------------------------------------------------------------------

async function assignTab(tabId) {
  try {
    var tab = await chrome.tabs.get(tabId);
    if (!tab || !tab.url || !tab.url.includes("chatgpt.com")) {
      return { success: false, error: "Not a ChatGPT page" };
    }

    assignedTabId = tabId;
    await saveState();

    // Notify content script (may not be loaded yet — that's ok,
    // ensureContentScript below will handle injection).
    try {
      await chrome.tabs.sendMessage(tabId, { action: "tab_assigned" });
    } catch (e) {
      // Content script may not be loaded yet — that's ok
    }

    // Ensure content script is available before advertising the tab.
    try {
      await ensureContentScript(tabId);
      console.log("[chatgpt-bridge] Content script verified in assigned tab", tabId);
    } catch (err) {
      console.warn("[chatgpt-bridge] Content script not ready in assigned tab", tabId, ":", err.message);
      // Still assign the tab but note the issue — handlePrompt will retry.
    }

    // Notify server
    safeWsSend({
      type: "tab_changed",
      tabId: tabId,
      url: tab.url,
    });

    return { success: true, tabId: tabId, url: tab.url };
  } catch (err) {
    return { success: false, error: err.message };
  }
}

async function unassignTab() {
  assignedTabId = null;
  await saveState();
  safeWsSend({
    type: "tab_changed",
    tabId: null,
    url: "",
  });
  return { success: true };
}

// ------------------------------------------------------------------
// Extension runtime messages (from popup)
// ------------------------------------------------------------------

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  // Relay content script diagnostics to the server
  if (request.action === "diagnostic") {
    var stage = request.stage || "";
    var message = request.message || "";
    var diagRequestId = request.requestId || "";
    console.log("[DIAGNOSTIC from content req=" + diagRequestId + "] stage=" + stage + " message=" + message);
    // Send diagnostic to server
    safeWsSend({
      type: "diagnostic",
      requestId: diagRequestId,
      component: "content",
      stage: stage,
      message: message,
    });
    return false;
  }

  if (request.action === "assign_tab") {
    assignTab(request.tabId).then(sendResponse);
    return true;
  }

  if (request.action === "unassign_tab") {
    unassignTab().then(sendResponse);
    return true;
  }

  if (request.action === "get_status") {
    sendResponse({
      wsConnected: ws && ws.readyState === WebSocket.OPEN,
      bridgeEnabled: bridgeEnabled,
      assignedTabId: assignedTabId,
    });
    return false;
  }

  if (request.action === "toggle_bridge") {
    bridgeEnabled = request.enabled;
    saveState();
    safeWsSend({ type: "bridge_state", enabled: bridgeEnabled });
    sendResponse({ success: true, enabled: bridgeEnabled });
    return false;
  }

  if (request.action === "get_token") {
    sendResponse({ token: wsToken });
    return false;
  }

  if (request.action === "set_token") {
    wsToken = request.token;
    saveState();
    sendResponse({ success: true });
    return false;
  }
});

// ------------------------------------------------------------------
// Tab events
// ------------------------------------------------------------------

chrome.tabs.onUpdated.addListener(function (tabId, changeInfo, tab) {
  if (assignedTabId && tabId === assignedTabId && changeInfo.status === "complete") {
    if (!tab.url || !tab.url.includes("chatgpt.com")) {
      console.log("[chatgpt-bridge] Assigned tab navigated away from ChatGPT");
    }
  }
});

chrome.tabs.onRemoved.addListener(function (tabId) {
  if (assignedTabId === tabId) {
    console.log("[chatgpt-bridge] Assigned tab was closed");
    assignedTabId = null;
    saveState();
    safeWsSend({
      type: "tab_changed",
      tabId: null,
      url: "",
    });
  }
});

// ------------------------------------------------------------------
// Service Worker lifecycle
// ------------------------------------------------------------------

chrome.runtime.onStartup.addListener(function () {
  console.log("[chatgpt-bridge] Service worker started");
  loadState().then(function () {
    connectWebSocket();
  });
});

chrome.runtime.onInstalled.addListener(function () {
  console.log("[chatgpt-bridge] Extension installed/updated");
  loadState().then(function () {
    connectWebSocket();
  });
});

// Periodic keep-alive (service workers can be killed)
// Only sends pings — reconnect is handled by onclose/scheduleReconnect.
// Skips reconnect if wsReconnectLocked is true (active request in flight).
setInterval(function () {
  if (wsReconnectLocked) {
    // Don't reconnect while a request is in flight — onclose will handle it.
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN && wsGeneration > 0) {
    safeWsSend({ type: "ping" });
  } else if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.log("[chatgpt-bridge] Connection lost, reconnecting...");
    connectWebSocket();
  }
}, 15000);

// ------------------------------------------------------------------
// Token fetch — get token from server on first connect
// ------------------------------------------------------------------

async function fetchToken() {
  if (wsToken) {
    // Token already persisted
    return;
  }
  try {
    var resp = await fetch("http://127.0.0.1:8766/token");
    if (resp.ok) {
      wsToken = await resp.text();
      wsToken = wsToken.trim();
      await saveState();
      console.log("[chatgpt-bridge] Fetched token from server");
    }
  } catch (err) {
    console.warn("[chatgpt-bridge] Could not fetch token:", err.message);
  }
}

// ------------------------------------------------------------------
// Initialize
// ------------------------------------------------------------------

loadState().then(function () {
  console.log("[chatgpt-bridge] Extension initialized");
  return fetchToken();
}).then(function () {
  connectWebSocket();
});
