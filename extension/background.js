/**
 * ChatGPT Bridge — Background service worker (Manifest V3).
 *
 * Responsibilities:
 * - WebSocket connection to localhost bridge server
 * - Reconnection with backoff
 * - Tab assignment management (persisted)
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
let wsToken = "";
let assignedTabId = null;
let bridgeEnabled = true;
let reconnectAttempts = 0;
let reconnectTimer = null;

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

function connectWebSocket() {
  if (ws) {
    try { ws.close(); } catch (e) { /* ignore */ }
    ws = null;
  }

  var url = SERVER_URL + (wsToken ? "?token=" + wsToken : "");
  console.log("[chatgpt-bridge] Connecting to", url.replace(/token=[^&]*/, "token=***"));

  try {
    ws = new WebSocket(url);
  } catch (err) {
    console.error("[chatgpt-bridge] WebSocket creation failed:", err);
    scheduleReconnect();
    return;
  }

  ws.onopen = function () {
    console.log("[chatgpt-bridge] WebSocket connected");
    reconnectAttempts = 0;

    ws.send(JSON.stringify({
      type: "hello",
      extensionVersion: "0.1.0",
    }));
  };

  ws.onmessage = function (event) {
    handleMessage(event.data);
  };

  ws.onclose = function (event) {
    console.log("[chatgpt-bridge] WebSocket closed:", event.code, event.reason);
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = function (err) {
    console.error("[chatgpt-bridge] WebSocket error:", err);
  };
}

function scheduleReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  var delay = Math.min(
    RECONNECT_BASE_MS * Math.pow(RECONNECT_FACTOR, reconnectAttempts),
    RECONNECT_MAX_MS
  );
  reconnectAttempts++;

  console.log("[chatgpt-bridge] Reconnecting in", delay, "ms (attempt", reconnectAttempts + 1, ")");
  reconnectTimer = setTimeout(function () {
    connectWebSocket();
  }, delay);
}

function sendMessage(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
    return true;
  }
  return false;
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
// Prompt handling
// ------------------------------------------------------------------

async function handlePrompt(msg) {
  var requestId = msg.requestId;
  var prompt = msg.prompt;
  var timeout = msg.timeout || 300000;

  // Validate bridge state
  if (!bridgeEnabled) {
    sendResponse(requestId, null, {
      success: false,
      error: "Bridge is disabled",
      code: "BRIDGE_DISABLED",
    });
    return;
  }

  if (!assignedTabId) {
    sendResponse(requestId, null, {
      success: false,
      error: "No ChatGPT tab assigned",
      code: "NO_CHATGPT_TAB",
    });
    return;
  }

  // Send accepted back to server
  sendMessage({
    type: "accepted",
    requestId: requestId,
  });

  // Validate tab exists
  try {
    var tab = await chrome.tabs.get(assignedTabId);
    if (!tab) {
      throw new Error("Tab not found");
    }
  } catch (err) {
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
    sendResponse(requestId, null, {
      success: false,
      error: "Assigned tab is not a ChatGPT page",
      code: "CHATGPT_NOT_READY",
    });
    return;
  }

  // Send prompt to content script
  try {
    var result = await chrome.tabs.sendMessage(assignedTabId, {
      action: "submit_prompt",
      prompt: prompt,
    }, { timeout: timeout });

    sendResponse(requestId, result, {
      success: result.success,
      response: result.response || "",
      url: tab.url,
    });

  } catch (err) {
    console.error("[chatgpt-bridge] Content script error:", err.message);

    // Try to re-check if tab is still valid
    try {
      var checkResult = await chrome.tabs.sendMessage(assignedTabId, {
        action: "check_tab",
      }, { timeout: 2000 });

      if (!checkResult || !checkResult.isChatGPT) {
        sendResponse(requestId, null, {
          success: false,
          error: "Assigned tab is not ChatGPT",
          code: "CHATGPT_NOT_READY",
        });
        return;
      }
    } catch (checkErr) {
      // Tab might be closed or unresponsive
      sendResponse(requestId, null, {
        success: false,
        error: "Content script communication failed: " + err.message,
        code: "CHATGPT_COMPOSER_NOT_FOUND",
      });
      return;
    }

    sendResponse(requestId, null, {
      success: false,
      error: "Timeout or error: " + err.message,
      code: "CHATGPT_TIMEOUT",
    });
  }
}

function sendResponse(requestId, result, serverMsg) {
  if (result && result.success) {
    serverMsg = serverMsg || {
      type: "response",
      requestId: requestId,
      response: result.response,
      url: result.url || "",
    };
  } else {
    serverMsg = serverMsg || {
      type: "error",
      requestId: requestId,
      code: result ? result.code || "UNKNOWN_ERROR" : "UNKNOWN_ERROR",
      message: result ? result.error || "Unknown error" : "Unknown error",
    };
  }
  sendMessage(serverMsg);
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

    // Notify content script
    try {
      await chrome.tabs.sendMessage(tabId, { action: "tab_assigned" });
    } catch (e) {
      // Content script may not be loaded yet — that's ok
    }

    // Notify server
    sendMessage({
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
  sendMessage({
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
    sendMessage({ type: "bridge_state", enabled: bridgeEnabled });
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
    sendMessage({
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
setInterval(function () {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.log("[chatgpt-bridge] Connection lost, reconnecting...");
    connectWebSocket();
  } else {
    sendMessage({ type: "ping" });
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
