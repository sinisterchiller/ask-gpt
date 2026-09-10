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
var stabilizationTimer = null;
var stateTimer = null;

// ------------------------------------------------------------------
// Message handling
// ------------------------------------------------------------------

chrome.runtime.onMessage.addListener(function (request, sender, sendResponse) {
  if (request.action === "submit_prompt") {
    submitPrompt(request.prompt).then(function (result) {
      sendResponse(result);
    }).catch(function (err) {
      sendResponse({ success: false, error: err.message, code: err.code || "UNKNOWN_ERROR" });
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
    sendResponse({
      isChatGPT: isChatGPTPage(),
      ready: isChatGPTReady(),
    });
    return false;
  }

  sendResponse({ success: false, error: "Unknown action" });
});

// ------------------------------------------------------------------
// Public API
// ------------------------------------------------------------------

function submitPrompt(prompt) {
  return new Promise(function (resolve, reject) {
    resetState();
    state = "SUBMITTED";

    try {
      if (!isChatGPTReady()) {
        throw createError("CHATGPT_NOT_READY", "ChatGPT page is not ready");
      }

      var composer = getComposer();
      if (!composer) {
        throw createError("CHATGPT_COMPOSER_NOT_FOUND", "Could not find composer");
      }

      lastMessageCount = SELECTORS.assistantMessages().length;

      // Insert text
      setComposerText(composer, prompt);

      // Wait for React to re-render (send button changes from voice to send icon)
      // and for the input events to propagate
      setTimeout(function () {
        var sendButton = getSendButton();
        if (!sendButton) {
          reject(createError("CHATGPT_SEND_BUTTON_NOT_FOUND", "Could not find send button"));
          return;
        }

        // Click the send button
        sendButton.click();

        // Also try Enter key as fallback
        composer.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter",
          code: "Enter",
          bubbles: true,
          cancelable: true,
        }));

        waitForResponse().then(resolve).catch(reject);
      }, 200);

    } catch (err) {
      state = "ERROR";
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

function waitForResponse() {
  return new Promise(function (resolve, reject) {
    var timeoutMs = 300000;
    var timeoutHandle = setTimeout(function () {
      cleanup();
      reject(createError("CHATGPT_TIMEOUT", "No response received within timeout"));
    }, timeoutMs);

    startObservation();

    var pollInterval = setInterval(function () {
      var newCount = SELECTORS.assistantMessages().length;

      if (newCount > lastMessageCount) {
        clearInterval(pollInterval);
        var messages = SELECTORS.assistantMessages();
        var newMsgEl = messages[messages.length - 1];
        if (newMsgEl) {
          startGenerationObserver(newMsgEl, function (text) {
            cleanup();
            clearTimeout(timeoutHandle);
            state = "COMPLETE";
            resolve({ success: true, response: text });
          });
        }
      } else {
        var errorEl = getErrorElement();
        if (errorEl && errorEl.offsetParent !== null) {
          clearInterval(pollInterval);
          cleanup();
          reject(createError("CHATGPT_GENERATION_ERROR", "ChatGPT encountered an error"));
        }
      }
    }, 500);
  });
}

function startGenerationObserver(messageEl, onComplete) {
  var lastText = "";
  var stableCount = 0;

  mutationObserver = new MutationObserver(function (mutations) {
    var currentText = messageEl.innerText || "";

    var stopBtn = getStopButton();
    var stopVisible = stopBtn !== null;

    if (currentText !== lastText) {
      if (state !== "GENERATING") {
        state = "GENERATING";
      }
      lastText = currentText;
      stableCount = 0;
    }

    if (stopVisible && state === "GENERATING") {
      // Still generating
    } else if (!stopVisible && currentText === lastText && state === "GENERATING") {
      stableCount++;
      if (stableCount >= 3) {
        stabilizationTimer = setTimeout(function () {
          var finalText = messageEl.innerText || "";
          onComplete(finalText);
        }, 1000);
      }
    }
  });

  mutationObserver.observe(document.body, {
    childList: true,
    characterData: true,
    subtree: true,
  });
}

function startObservation() {
  stateTimer = setInterval(function () {
    var stopBtn = getStopButton();
    if (stopBtn === null && state === "GENERATING") {
      state = "STABILIZING";
    }
  }, 1000);
}

function cleanup() {
  if (mutationObserver) {
    mutationObserver.disconnect();
    mutationObserver = null;
  }
  if (stabilizationTimer) {
    clearTimeout(stabilizationTimer);
    stabilizationTimer = null;
  }
  if (stateTimer) {
    clearTimeout(stateTimer);
    stateTimer = null;
  }
}

function resetState() {
  state = "IDLE";
  cleanup();
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
