/**
 * ChatGPT Bridge — Popup UI controller.
 *
 * Manages the extension popup: tab assignment, bridge enable/disable,
 * connection status display.
 */

"use strict";

// ------------------------------------------------------------------
// DOM elements
// ------------------------------------------------------------------

var serverDot = document.getElementById("server-dot");
var serverText = document.getElementById("server-text");
var currentTabTitle = document.getElementById("current-tab-title");
var btnAssign = document.getElementById("btn-assign");
var assignedTabStatus = document.getElementById("assigned-tab-status");
var assignedTabIdEl = document.getElementById("assigned-tab-id");
var btnOpen = document.getElementById("btn-open");
var btnUnassign = document.getElementById("btn-unassign");
var bridgeDot = document.getElementById("bridge-dot");
var bridgeText = document.getElementById("bridge-text");
var btnToggle = document.getElementById("btn-toggle");
var tokenDisplay = document.getElementById("token-display");

// ------------------------------------------------------------------
// Initialization
// ------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", function () {
  updateStatus();
  refreshCurrentTab();
  refreshAssignedTab();
  displayToken();

  // Refresh current tab periodically
  setInterval(refreshCurrentTab, 2000);

  // Refresh status periodically
  setInterval(updateStatus, 3000);
});

// ------------------------------------------------------------------
// Event handlers
// ------------------------------------------------------------------

btnAssign.addEventListener("click", function () {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (tabs.length === 0) return;

    var tab = tabs[0];
    chrome.runtime.sendMessage({
      action: "assign_tab",
      tabId: tab.id,
    }, function (response) {
      if (chrome.runtime.lastError) {
        alert("Error: " + chrome.runtime.lastError.message);
        return;
      }
      if (response && response.success) {
        refreshAssignedTab();
      } else {
        alert("Failed: " + (response ? response.error : "Unknown error"));
      }
    });
  });
});

btnUnassign.addEventListener("click", function () {
  chrome.runtime.sendMessage({ action: "unassign_tab" }, function (response) {
    if (chrome.runtime.lastError) {
      alert("Error: " + chrome.runtime.lastError.message);
      return;
    }
    refreshAssignedTab();
  });
});

btnOpen.addEventListener("click", function () {
  chrome.tabs.get(assignedTabIdEl.textContent, function (tab) {
    if (chrome.runtime.lastError) {
      alert("Tab not found");
      return;
    }
    chrome.tabs.update(tab.id, { active: true });
    chrome.windows.update(tab.windowId, { focused: true });
  });
});

btnToggle.addEventListener("click", function () {
  var newState = bridgeText.textContent !== "Enabled";
  chrome.runtime.sendMessage({
    action: "toggle_bridge",
    enabled: newState,
  }, function (response) {
    if (chrome.runtime.lastError) {
      alert("Error: " + chrome.runtime.lastError.message);
      return;
    }
    refreshAssignedTab();
  });
});

// ------------------------------------------------------------------
// Status updates
// ------------------------------------------------------------------

function updateStatus() {
  chrome.runtime.sendMessage({ action: "get_status" }, function (response) {
    if (chrome.runtime.lastError) {
      setServerStatus(false, "Error");
      return;
    }

    // Server status
    if (response && response.wsConnected) {
      setServerStatus(true, "Connected");
    } else {
      setServerStatus(false, "Disconnected");
    }

    // Bridge status
    if (response && response.bridgeEnabled !== undefined) {
      if (response.bridgeEnabled) {
        bridgeDot.className = "dot dot-green";
        bridgeText.textContent = "Enabled";
        btnToggle.textContent = "Disable Bridge";
      } else {
        bridgeDot.className = "dot dot-red";
        bridgeText.textContent = "Disabled";
        btnToggle.textContent = "Enable Bridge";
      }
    }
  });
}

function refreshCurrentTab() {
  chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
    if (tabs.length === 0) return;
    var tab = tabs[0];
    var title = tab.title || "Untitled";
    var isChatGPT = tab.url && tab.url.includes("chatgpt.com");

    currentTabTitle.textContent = title + (isChatGPT ? " (ChatGPT)" : "");
    currentTabTitle.style.color = isChatGPT ? "#28a745" : "#6c757d";
  });
}

function refreshAssignedTab() {
  chrome.runtime.sendMessage({ action: "get_status" }, function (response) {
    if (chrome.runtime.lastError) return;

    if (response && response.assignedTabId) {
      assignedTabStatus.textContent = "ChatGPT";
      assignedTabStatus.style.color = "#28a745";
      assignedTabIdEl.textContent = "Tab ID: " + response.assignedTabId;
      btnOpen.disabled = false;
      btnUnassign.disabled = false;
    } else {
      assignedTabStatus.textContent = "Not assigned";
      assignedTabStatus.style.color = "#6c757d";
      assignedTabIdEl.textContent = "";
      btnOpen.disabled = true;
      btnUnassign.disabled = true;
    }
  });
}

function setServerStatus(connected, text) {
  if (connected) {
    serverDot.className = "dot dot-green";
  } else {
    serverDot.className = "dot dot-red";
  }
  serverText.textContent = text;
}

function displayToken() {
  chrome.storage.local.get(["wsToken"], function (stored) {
    if (stored.wsToken) {
      tokenDisplay.textContent = stored.wsToken.substring(0, 16) + "…";
    } else {
      tokenDisplay.textContent = "Not set";
    }
  });
}
