/**
 * ChatGPT Bridge — Content script (runs inside the assigned ChatGPT tab).
 *
 * Responsibilities:
 * - Detect ChatGPT page readiness
 * - Find composer and send button
 * - Insert and submit prompts
 * - Observe generation via MutationObserver
 * - Extract and return the final response
 */

"use strict";

// ------------------------------------------------------------------
// Initialization guard — prevent duplicate listeners on re-injection
// ------------------------------------------------------------------

if (globalThis.__chatgptBridgeListenerRegistered) {
  // Listener already registered (survives re-injection).
  console.log("[chatgpt-bridge] Listener already registered, skipping.");
} else {
  globalThis.__chatgptBridgeListenerRegistered = true;
  globalThis.__chatgptBridgeInitialized = true;

  // All content-script code goes inside this block.
  initContentScript();
}

function initContentScript() {

// ------------------------------------------------------------------
// Selectors — isolated from the rest of the extension
// ------------------------------------------------------------------

var SELECTORS = {
  composer: function () {
    var sels = [
      // New ChatGPT: contenteditable ProseMirror with id prompt-textarea
      '#prompt-textarea',
      'div.ProseMirror[contenteditable="true"][role="textbox"]',
      // Fallback: contenteditable near composer surface
      '[data-composer-surface="true"] [contenteditable="true"]',
      '[data-composer-body=""] [contenteditable="true"]',
      // Legacy textarea
      'textarea[name="prompt-textarea"]',
      'textarea[placeholder*="Ask ChatGPT"]',
      'textarea[placeholder*="Message ChatGPT"]',
      // Generic fallback
      '[contenteditable="true"][role="textbox"]',
      'textarea',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) return el;
    }
    return null;
  },

  sendButton: function () {
    var sels = [
      // New ChatGPT: submit button in trailing area (appears when text is entered)
      '[data-composer-transition-slot="trailing"] button.composer-submit-button-color',
      'button.composer-submit-button-color',
      // Fallback: send/submit buttons
      'button[aria-label*="Send"]',
      'button[aria-label*="submit"]',
      'button[data-testid*="send"]',
      // Legacy
      'form button[type="submit"]',
      'button.send-button',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) return el;
    }
    return null;
  },

  stopButton: function () {
    var sels = [
      // New ChatGPT: stop generation button
      'button[aria-label*="Stop response"]',
      'button[aria-label*="stop generation"]',
      'button[aria-label*="Stop"]',
      'button[data-testid*="stop"]',
      // Fallback
      'button[aria-label*="stop"]',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) return el;
    }
    return null;
  },

  assistantMessages: function () {
    // New ChatGPT uses data-message-author-role
    var els = document.querySelectorAll('[data-message-author-role="assistant"]');
    if (els.length > 0) return els;
    // Fallback: look for assistant message containers
    return document.querySelectorAll('[class*="assistant-message"]');
  },

  errorBanner: function () {
    var sels = [
      '[class*="error-banner"]',
      '[class*="error-message"]',
      '[class*="generation-error"]',
    ];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) return el;
    }
    return null;
  },
};

// ------------------------------------------------------------------
// State
// ------------------------------------------------------------------

var state = "IDLE";
var lastMessageCount = 0;
var mutationObserver = null;
var stateTimer = null;

// Shared by waitForResponse and startObservation
var messageEl = null;
var stableCount = 0;
var lastRequestId = null;
var lastText = "";
var responseResolve = null; // Promise resolve from waitForResponse
var responseTimeoutHandle = null;
var generationStartAt = 0; // Timestamp when generation observation started

// Synchronous lock to prevent duplicate submissions from multiple listeners.
// Stored on globalThis so it survives re-injection (each executeScript creates
// a new scope with its own copy of module-level variables).
var processingLock = globalThis.__chatgptBridgeProcessingLock || false;

// ------------------------------------------------------------------
// Message handling
// ------------------------------------------------------------------

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "submit_prompt") {
    var diagRequestId = request.requestId || "unknown";
    console.log("[content script] submit_prompt received");
    chrome.runtime.sendMessage({
      action: "diagnostic",
      requestId: diagRequestId,
      stage: "content_script_received",
      message: "prompt_len=" + (request.prompt || "").length,
    });
    // Synchronous lock — prevents duplicate submissions even when
    // multiple listeners fire in the same event loop tick.
    if (processingLock) {
      console.log("[content script] submit_prompt ignored — lock held");
      sendResponse({ success: false, error: "Already processing a request", code: "ALREADY_PROCESSING" });
      return false;
    }
    processingLock = true;
    globalThis.__chatgptBridgeProcessingLock = true;

    submitPrompt(diagRequestId, request.prompt).then(function (result) {
      console.log("[content script] submit_prompt result:", result);
      chrome.runtime.sendMessage({
        action: "diagnostic",
        requestId: diagRequestId,
        stage: "content_response_sent",
        message: "success=" + (result ? result.success : "null"),
      });
      sendResponse(result);
    }).catch(function (err) {
      console.log("[content script] submit_prompt error:", err.message);
      chrome.runtime.sendMessage({
        action: "diagnostic",
        requestId: diagRequestId,
        stage: "content_error",
        message: err.message,
      });
      sendResponse({ success: false, error: err.message, code: err.code || "UNKNOWN_ERROR" });
    }).finally(function () {
      processingLock = false;
      globalThis.__chatgptBridgeProcessingLock = false;
    });
    return true;
  }

  if (request.action === "get_state") {
    sendResponse({
      ready: isChatGPTReady(),
      composerFound: !!getComposer(),
      sendButtonFound: !!getSendButton(),
    });
    return false;
  }

  if (request.action === "check_tab") {
    console.log("[content script] check_tab: isChatGPT=" + isChatGPTPage() + " ready=" + isChatGPTReady());
    sendResponse({
      isChatGPT: isChatGPTPage(),
      ready: isChatGPTReady(),
    });
    return false;
  }

  if (request.action === "tab_assigned") {
    console.log("[content script] tab_assigned received");
    sendResponse({ ok: true });
    return false;
  }

  sendResponse({ success: false, error: "Unknown action" });
});

// ------------------------------------------------------------------
// Public API
// ------------------------------------------------------------------

function submitPrompt(requestId, prompt) {
  return new Promise(function (resolve, reject) {
    resetState();
    state = "SUBMITTED";

    chrome.runtime.sendMessage({
      action: "diagnostic",
      requestId: requestId,
      stage: "submit_prompt_started",
    });

    try {
      if (!isChatGPTReady()) {
        var isChatGPT = isChatGPTPage();
        var composerEl = getComposer();
        chrome.runtime.sendMessage({
          action: "diagnostic",
          requestId: requestId,
          stage: "not_ready",
          message: "isChatGPT=" + isChatGPT + " composer=" + (composerEl !== null),
        });
        throw createError("CHATGPT_NOT_READY", "ChatGPT page is not ready");
      }

      var composer = getComposer();
      if (!composer) {
        chrome.runtime.sendMessage({
          action: "diagnostic",
          requestId: requestId,
          stage: "composer_not_found",
        });
        throw createError("CHATGPT_COMPOSER_NOT_FOUND", "Could not find composer");
      }

      chrome.runtime.sendMessage({
        action: "diagnostic",
      requestId: requestId,
        stage: "composer_found",
        message: "type=" + composer.tagName + " className=" + (composer.className || "").substring(0, 50),
      });
      lastMessageCount = SELECTORS.assistantMessages().length;
      chrome.runtime.sendMessage({
        action: "diagnostic",
      requestId: requestId,
        stage: "message_count_recorded",
        message: "count=" + lastMessageCount,
      });

      // Insert text
      setComposerText(composer, prompt);

      // Verify insertion
      var insertedText = composer.innerText || composer.value || "";
      var insertionOk = insertedText.includes(prompt.substring(0, Math.min(20, prompt.length)));
      chrome.runtime.sendMessage({
        action: "diagnostic",
      requestId: requestId,
        stage: "prompt_inserted",
        message: "insertion_ok=" + insertionOk + " text_preview=" + insertedText.substring(0, 50),
      });

      if (!insertionOk && insertedText.length > 0) {
        chrome.runtime.sendMessage({
          action: "diagnostic",
      requestId: requestId,
          stage: "insertion_mismatch",
          message: "expected_contains=" + prompt.substring(0, 20) + " got=" + insertedText.substring(0, 50),
        });
      }

      // Wait for React to re-render (send button changes from voice to send icon)
      // and for the input events to propagate
      setTimeout(function () {
        var sendButton = getSendButton();
        if (!sendButton) {
          chrome.runtime.sendMessage({
            action: "diagnostic",
      requestId: requestId,
            stage: "send_button_not_found",
          });
          reject(createError("CHATGPT_SEND_BUTTON_NOT_FOUND", "Could not find send button"));
          return;
        }

        chrome.runtime.sendMessage({
          action: "diagnostic",
      requestId: requestId,
          stage: "send_button_found",
          message: "ariaLabel=" + (sendButton.getAttribute("aria-label") || "").substring(0, 50),
        });

        // Click the send button — sufficient to submit
        sendButton.click();
        chrome.runtime.sendMessage({
          action: "diagnostic",
      requestId: requestId,
          stage: "send_triggered",
        });

        waitForResponse(requestId).then(resolve).catch(reject);
      }, 200);

    } catch (err) {
      state = "ERROR";
      chrome.runtime.sendMessage({
        action: "diagnostic",
      requestId: requestId,
        stage: "submit_error",
        message: err.message,
      });
      reject(err);
    }
  });
}

// ------------------------------------------------------------------
// Core functions
// ------------------------------------------------------------------

function isChatGPTPage() {
  return window.location.hostname.includes("chatgpt.com");
}

function isChatGPTReady() {
  return isChatGPTPage() && getComposer() !== null;
}

function getComposer() {
  return SELECTORS.composer();
}

function getSendButton() {
  return SELECTORS.sendButton();
}

function getStopButton() {
  return SELECTORS.stopButton();
}

function getErrorElement() {
  return SELECTORS.errorBanner();
}

function setComposerText(composer, text) {
  // Handle hidden textarea fallback
  var textarea = composer.querySelector('textarea[name="prompt-textarea"]');
  if (textarea && textarea.style.display !== "none") {
    textarea.value = text;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.dispatchEvent(new Event("change", { bubbles: true }));
    textarea.focus();
    return;
  }

  if (composer.tagName === "TEXTAREA") {
    composer.value = text;
    composer.dispatchEvent(new Event("input", { bubbles: true }));
    composer.dispatchEvent(new Event("change", { bubbles: true }));
    composer.focus();
    return;
  }

  // ProseMirror contenteditable div — use execCommand for reliable insertion
  composer.focus();

  // Clear existing content first
  composer.innerHTML = "";

  // Use execCommand which ProseMirror intercepts
  document.execCommand("insertText", false, text);

  // Also dispatch input events for React/React-like frameworks
  composer.dispatchEvent(new InputEvent("beforeinput", {
    bubbles: true,
    cancelable: true,
    inputType: "insertText",
    data: text,
  }));

  composer.dispatchEvent(new Event("input", { bubbles: true }));
  composer.dispatchEvent(new Event("change", { bubbles: true }));
}

function waitForResponse(requestId) {
  return new Promise(function (resolve, reject) {
    var timeoutMs = 300000;

    // Set module-level state for startObservation to access
    messageEl = null;
    stableCount = 0;
    lastText = "";
    lastRequestId = requestId;
    responseResolve = resolve;
    generationStartAt = Date.now();
    responseTimeoutHandle = setTimeout(function () {
      cleanup();
      chrome.runtime.sendMessage({
        action: "diagnostic",
      requestId: requestId,
        stage: "response_timeout",
        message: "timeout_ms=" + timeoutMs,
      });
      reject(createError("CHATGPT_TIMEOUT", "No response received within timeout"));
    }, timeoutMs);

    chrome.runtime.sendMessage({
      action: "diagnostic",
      requestId: requestId,
      stage: "waiting_for_response",
    });

    startObservation();

    var pollInterval = setInterval(function () {
      var newCount = SELECTORS.assistantMessages().length;

      if (newCount > lastMessageCount) {
        chrome.runtime.sendMessage({
          action: "diagnostic",
      requestId: requestId,
          stage: "assistant_detected",
          message: "count=" + newCount + " lastMessageCount=" + lastMessageCount,
        });
        clearInterval(pollInterval);
        var messages = SELECTORS.assistantMessages();
        // ChatGPT renders newest messages first in the DOM
        var newMsgEl = messages[0];
        if (newMsgEl) {
          // Set module-level for startObservation
          messageEl = newMsgEl;
          chrome.runtime.sendMessage({
            action: "diagnostic",
      requestId: requestId,
            stage: "assistant_message_element_found",
          });
        }
      } else {
        var errorEl = getErrorElement();
        if (errorEl && errorEl.offsetParent !== null) {
          clearInterval(pollInterval);
          cleanup();
          chrome.runtime.sendMessage({
            action: "diagnostic",
      requestId: requestId,
            stage: "generation_error_detected",
          });
          reject(createError("CHATGPT_GENERATION_ERROR", "ChatGPT encountered an error"));
        }
      }
    }, 500);
  });
}

function startObservation() {
  // Track message count to detect when a new message appears.
  // ChatGPT renders messages oldest-first in the DOM.
  // When count increases, the NEW message is at the END of the NodeList.
  var prevMsgCount = 0;

  stateTimer = setInterval(function () {
    var stopBtn = getStopButton();
    var stopVisible = false;
    if (stopBtn) {
      try {
        var style = window.getComputedStyle(stopBtn);
        stopVisible = style.display !== "none" &&
                      style.visibility !== "hidden" &&
                      style.opacity !== "0";
      } catch (e) {
        stopVisible = true;
      }
    }

    if (stopVisible) {
      // Still generating
      return;
    }

    // Re-query assistant messages.
    var allMsgs = SELECTORS.assistantMessages();
    if (!allMsgs || allMsgs.length === 0) return;
    var newMsgCount = allMsgs.length;

    // When a new message appears, pick the LAST one (newest).
    // Skip "Thinking" indicators which may be at the end.
    if (newMsgCount > prevMsgCount) {
      prevMsgCount = newMsgCount;
      // Walk backwards from the end, skip "Thinking" elements
      for (var i = newMsgCount - 1; i >= 0; i--) {
        var candidate = allMsgs[i];
        var candidateText = candidate.innerText || "";
        // Skip "Thinking" or very short elements
        if (candidateText.length > 10) {
          messageEl = candidate;
          lastText = "";
          stableCount = 0;
          break;
        }
      }
    }

    if (!messageEl) return;

    var currentText = messageEl.innerText || "";
    var elapsed = Date.now() - generationStartAt;

    // Diagnostic: log stabilization state every 3 checks
    if (!startObservation._diagnosticCounter) startObservation._diagnosticCounter = 0;
    startObservation._diagnosticCounter++;
    if (startObservation._diagnosticCounter % 3 === 0) {
      chrome.runtime.sendMessage({
        action: "diagnostic",
        requestId: lastRequestId,
        stage: "stabilization_debug",
        message: "elapsed=" + elapsed + " text_len=" + currentText.length +
          " lastText_len=" + lastText.length +
          " equal=" + (currentText === lastText) +
          " len_gte_20=" + (currentText.length >= 20) +
          " state=" + state +
          " stableCount=" + stableCount +
          " msgCount=" + newMsgCount,
      });
    }

    // Require at least 2s of observation before stabilization.
    // ChatGPT's stop button can disappear mid-stream during the
    // "Thinking" phase, causing premature stabilization.
    if (elapsed < 2000) return;
    if (currentText === lastText && currentText.length >= 20 && state === "GENERATING") {
      stableCount++;
      if (stableCount >= 3) {
        clearInterval(stateTimer);
        stateTimer = null;
        cleanup();
        clearTimeout(responseTimeoutHandle);
        chrome.runtime.sendMessage({
          action: "diagnostic",
          requestId: lastRequestId,
          stage: "stabilization_threshold_reached",
          message: "stable_count=" + stableCount + " text_len=" + currentText.length,
        });
        chrome.runtime.sendMessage({
          action: "diagnostic",
          requestId: lastRequestId,
          stage: "response_extracted",
          message: "length=" + currentText.length,
        });
        state = "COMPLETE";
        if (responseResolve) responseResolve({ success: true, response: currentText });
      }
    } else if (currentText !== lastText) {
      // Text changed — reset stabilization counter
      lastText = currentText;
      stableCount = 0;
      if (state !== "GENERATING") {
        state = "GENERATING";
      }
    }
  }, 1000);
}

function cleanup() {
  if (mutationObserver) {
    mutationObserver.disconnect();
    mutationObserver = null;
  }
  if (stateTimer) {
    clearTimeout(stateTimer);
    stateTimer = null;
  }
}

function resetState() {
  state = "IDLE";
  cleanup();
  messageEl = null;
  stableCount = 0;
  lastText = "";
  generationStartAt = 0;
}

function createError(code, message) {
  var err = new Error(message);
  err.code = code;
  return err;
}

// ------------------------------------------------------------------
// Log readiness
// ------------------------------------------------------------------
console.log("[chatgpt-bridge] Content script loaded on", window.location.href);

} // end initContentScript()
