/**
 * ChatGPT DOM selectors — isolated from the rest of the extension.
 *
 * If ChatGPT's frontend changes, update selectors here first.
 * Prefer semantic attributes over generated CSS class names.
 */

const SELECTORS = {
  // The main input/composer textarea or contenteditable
  composer: () => {
    // Try multiple strategies in order of preference
    const selectors = [
      // New ChatGPT: textarea with placeholder "Message ChatGPT"
      'textarea[placeholder*="Message ChatGPT"]',
      // contenteditable composer
      '[contenteditable][data-placeholder*="Message"]',
      // Generic textarea in the composer area
      'textarea.chatgpt-plaintext-input',
      'textarea.composer-textarea',
      // Fallback: any textarea in the main composer container
      'textarea',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  },

  // Send button
  sendButton: () => {
    const selectors = [
      // Button with send icon, often has aria-label
      'button[aria-label*="Send"]',
      'button[aria-label*="submit"]',
      // Button with a specific class pattern
      'button[data-testid*="send"]',
      'button.send-button',
      // Fallback: last button in the composer area
      'form button[type="submit"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  },

  // Stop generation button (used to detect when generation has ended)
  stopButton: () => {
    const selectors = [
      'button[aria-label*="stop"]',
      'button[aria-label*="Stop"]',
      'button[data-testid*="stop"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  },

  // Assistant response messages
  assistantMessages: () => {
    // Look for assistant message containers
    const selectors = [
      '[data-message-author-role="assistant"]',
      '[class*="assistant-message"]',
      '[class*="message--"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  },

  // All assistant messages (for tracking new messages)
  allAssistantMessages: () => {
    return document.querySelectorAll('[data-message-author-role="assistant"]');
  },

  // ChatGPT error banner
  errorBanner: () => {
    const selectors = [
      '[class*="error-banner"]',
      '[class*="error-message"]',
      '[class*="generation-error"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  },
};

// Export for use in content.js
if (typeof window !== 'undefined') {
  window.CHATGPT_SELECTORS = SELECTORS;
}
