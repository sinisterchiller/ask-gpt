# Claude Code ↔ ChatGPT Browser Bridge

## 1. Goal

Build a local tool that Claude Code can call to send a request to a user-assigned ChatGPT browser tab and receive ChatGPT's completed response back inside the Claude Code console.

The complete flow is:

```text
Claude Code
    ↓
MCP tool: ask_chatgpt(...)
    ↓
Local MCP / bridge server
    ↓
WebSocket
    ↓
Chrome extension
    ↓
Explicitly assigned ChatGPT tab
    ↓
ChatGPT generates response
    ↓
Chrome extension captures response
    ↓
Local bridge server
    ↓
MCP tool result
    ↓
Claude Code console
```

The first version should deliberately be small:

* 1 Claude Code client
* 1 local server
* 1 Chrome extension
* 1 manually assigned ChatGPT tab
* 1 active request at a time
* text prompts
* text responses
* persistent ChatGPT conversation
* no automatic filesystem access for ChatGPT

---

# 2. Architecture

```text
┌───────────────────────────┐
│       Claude Code         │
│                           │
│ MCP tool:                 │
│ ask_chatgpt(prompt)       │
└────────────┬──────────────┘
             │ MCP / stdio
             ▼
┌───────────────────────────┐
│ Local MCP / Bridge Server │
│                           │
│ - exposes MCP tool        │
│ - creates request IDs     │
│ - queues requests         │
│ - tracks pending calls    │
│ - handles timeouts        │
│ - hosts WebSocket server  │
└────────────┬──────────────┘
             │
             │ ws://127.0.0.1:8765
             ▼
┌───────────────────────────┐
│     Chrome Extension      │
│                           │
│ Background service worker │
│                           │
│ - maintains WebSocket     │
│ - stores assigned tab     │
│ - routes requests         │
│ - routes responses        │
└────────────┬──────────────┘
             │
             │ chrome.tabs.sendMessage()
             ▼
┌───────────────────────────┐
│ ChatGPT Content Script    │
│                           │
│ - finds composer          │
│ - inserts prompt          │
│ - submits prompt          │
│ - watches generation      │
│ - captures final answer   │
└────────────┬──────────────┘
             │
             ▼
┌───────────────────────────┐
│    Assigned ChatGPT Tab   │
│                           │
│       chatgpt.com         │
└───────────────────────────┘
```

Return path:

```text
ChatGPT
   ↓
content script
   ↓
extension background worker
   ↓
WebSocket
   ↓
local server
   ↓
MCP result
   ↓
Claude Code
```

---

# 3. Recommended Project Structure

```text
chatgpt-bridge/
├── server/
│   ├── main.py
│   ├── mcp_server.py
│   ├── websocket_server.py
│   ├── request_manager.py
│   ├── protocol.py
│   ├── config.py
│   └── models.py
│
├── extension/
│   ├── manifest.json
│   ├── background.js
│   ├── content.js
│   ├── popup.html
│   ├── popup.js
│   ├── styles.css
│   └── selectors.js
│
├── tests/
│   ├── test_protocol.py
│   ├── test_request_manager.py
│   └── test_queue.py
│
├── README.md
├── requirements.txt
└── .gitignore
```

Keep ChatGPT DOM selectors isolated in `selectors.js`.

That way a ChatGPT frontend change should require changing selector logic rather than rewriting the entire extension.

---

# 4. Claude Code Interface

Use MCP instead of making Claude manually invoke shell commands.

Initial MCP tool:

```text
ask_chatgpt
```

Suggested arguments:

```json
{
  "prompt": "Review this implementation and find bugs.",
  "timeout": 180
}
```

Suggested result:

```json
{
  "success": true,
  "response": "The main issue is...",
  "request_id": "req_8fb12",
  "conversation_url": "https://chatgpt.com/c/..."
}
```

Conceptually:

```python
@mcp.tool()
async def ask_chatgpt(prompt: str, timeout: int = 180):
    return await bridge.ask(
        prompt=prompt,
        timeout=timeout
    )
```

For the MVP, only implement:

```text
ask_chatgpt()
```

Possible future tools:

```text
get_chatgpt_status()
new_chatgpt_chat()
cancel_chatgpt_request()
ask_chatgpt_with_files()
```

---

# 5. Local MCP / Bridge Server

The same process performs two major roles.

## MCP side

```text
Claude Code
    ↓
MCP stdio
    ↓
bridge server
```

## Chrome side

```text
Chrome extension
    ↓
WebSocket
    ↓
bridge server
```

Recommended WebSocket endpoint:

```text
ws://127.0.0.1:8765/ws
```

The Chrome extension should initiate the connection.

Do not attempt to make the localhost application directly connect into the Chrome extension.

---

# 6. Request Lifecycle

Every request gets a unique ID.

Example:

```json
{
  "type": "prompt",
  "requestId": "req_01JXYZ",
  "prompt": "Analyze this code.",
  "timeout": 180000
}
```

The server tracks it:

```python
pending_requests = {
    "req_01JXYZ": future
}
```

Complete lifecycle:

```text
1. Claude calls ask_chatgpt(...).

2. MCP server validates the request.

3. Server checks:
   - extension is connected
   - bridge is enabled
   - assigned ChatGPT tab exists

4. Server generates request ID.

5. Request enters queue.

6. Request manager makes it active.

7. Prompt is sent over WebSocket.

8. Extension acknowledges request.

9. Extension forwards it to assigned tab.

10. Content script records current ChatGPT state.

11. Content script inserts prompt.

12. Content script submits prompt.

13. Content script waits for assistant response.

14. MutationObserver watches generation.

15. Completion is detected.

16. Response text is extracted.

17. Content script sends response to background worker.

18. Background worker sends response to localhost.

19. Localhost matches request ID.

20. Pending future resolves.

21. MCP tool result returns to Claude Code.
```

---

# 7. Chrome Extension

Use Manifest V3.

Main components:

```text
background.js
    │
    ├── WebSocket connection
    ├── reconnect handling
    ├── assigned tab state
    ├── request routing
    └── result routing

content.js
    │
    ├── ChatGPT readiness detection
    ├── composer detection
    ├── prompt insertion
    ├── submit
    ├── response observation
    └── response extraction

popup.html / popup.js
    │
    ├── connection state
    ├── assign current tab
    ├── unassign
    ├── open assigned tab
    └── enable/disable bridge
```

---

# 8. Explicit ChatGPT Tab Assignment

The extension must never guess which ChatGPT tab Claude should use.

The user explicitly assigns one.

Example popup:

```text
ChatGPT Bridge
────────────────────────

Server
● Connected

Current tab
ChatGPT

[ Assign Current Tab ]

Assigned tab
✓ ChatGPT
Tab ID: 918273

[ Open Assigned Tab ]
[ Unassign ]

Bridge
[ Disable Bridge ]
```

When assigned:

```javascript
assignedTabId = tab.id;
```

Use extension storage such as:

```javascript
chrome.storage.local
```

or:

```javascript
chrome.storage.session
```

Important behavior:

* validate the tab before each request
* detect a closed tab
* clear stale assignments
* reject non-ChatGPT pages
* never automatically attach to all ChatGPT tabs

---

# 9. WebSocket Protocol

Keep the protocol small.

## Extension handshake

```json
{
  "type": "hello",
  "extensionVersion": "0.1.0"
}
```

Server:

```json
{
  "type": "hello_ack",
  "protocolVersion": 1
}
```

## Prompt

```json
{
  "type": "prompt",
  "requestId": "req_123",
  "prompt": "Review this code.",
  "timeout": 180000
}
```

## Accepted

```json
{
  "type": "accepted",
  "requestId": "req_123"
}
```

## Response

```json
{
  "type": "response",
  "requestId": "req_123",
  "response": "I found three issues...",
  "url": "https://chatgpt.com/c/..."
}
```

## Error

```json
{
  "type": "error",
  "requestId": "req_123",
  "code": "NO_ASSIGNED_TAB",
  "message": "No ChatGPT tab is assigned."
}
```

---

# 10. Prompt Injection

The content script interacts with the ChatGPT composer.

Conceptually:

```javascript
async function submitPrompt(prompt) {
    const composer = findComposer();

    if (!composer) {
        throw new Error("CHATGPT_COMPOSER_NOT_FOUND");
    }

    setComposerText(composer, prompt);

    const sendButton = findSendButton();

    if (!sendButton) {
        throw new Error("CHATGPT_SEND_BUTTON_NOT_FOUND");
    }

    sendButton.click();
}
```

Do not depend heavily on generated CSS classes.

Avoid selectors like:

```text
.group\\/composer-parent > div > div:nth-child(...)
```

Prefer semantic selectors:

```text
role
aria-label
contenteditable
stable data-* attributes
button state
semantic DOM structure
```

Centralize them:

```javascript
const SELECTORS = {
    composer: "...",
    sendButton: "...",
    stopButton: "...",
    assistantMessages: "..."
};
```

The implementation must verify the current live ChatGPT DOM rather than assuming old selectors still work.

---

# 11. React / Input Event Handling

Simply doing:

```javascript
composer.textContent = prompt;
```

may not be sufficient.

The content script may need to trigger the events expected by ChatGPT's frontend.

Possible mechanisms include:

```text
focus
beforeinput
input
change
keyboard/input events
```

The exact behavior must be tested against the current UI.

Do not guess if it can be inspected directly.

---

# 12. Response Detection

Do not implement:

```text
submit
sleep 20 seconds
grab last message
```

Use event-driven observation.

Suggested states:

```text
IDLE
 ↓
SUBMITTED
 ↓
WAITING_FOR_ASSISTANT
 ↓
GENERATING
 ↓
STABILIZING
 ↓
COMPLETE
```

Recommended process:

```text
1. Record current assistant-message state.

2. Submit prompt.

3. Wait for a new assistant response.

4. Attach MutationObserver.

5. Track changes to response content.

6. Observe generation controls/state.

7. Detect ChatGPT errors.

8. Wait until generation has stopped.

9. Confirm text is stable for a short period.

10. Extract final response.
```

Use multiple signals rather than one fragile selector.

Potential completion signals:

* stop-generation control disappears
* send control returns to normal
* assistant response exists
* text stops changing
* no visible ChatGPT error
* response element remains stable

A short stabilization delay such as roughly:

```text
500–1000 ms
```

after an event-driven completion signal is reasonable.

---

# 13. Response Extraction

For MVP:

```javascript
const response = assistantElement.innerText;
```

This gives robust plain text.

Do not make perfect Markdown preservation a blocker.

Later improvements can preserve:

* headings
* lists
* inline code
* fenced code blocks
* tables
* links
* quotes

Potential future strategies:

```text
DOM → Markdown converter
ChatGPT copy-response representation
structured DOM traversal
```

---

# 14. Concurrency

Only one active request should use one ChatGPT tab.

```text
MAX_CONCURRENT_CHATGPT_REQUESTS = 1
```

Example:

```text
A → running
B → queued
C → queued
```

After A completes:

```text
B → running
C → queued
```

After B completes:

```text
C → running
```

This avoids ambiguity about which ChatGPT response belongs to which request.

Future multi-tab architecture:

```text
request manager
    ├── worker 1 → ChatGPT tab A
    ├── worker 2 → ChatGPT tab B
    └── worker 3 → ChatGPT tab C
```

Do not build that initially.

---

# 15. Persistent Conversation

Default behavior should use whatever ChatGPT conversation is currently assigned.

```text
Claude request #1
    ↓
ChatGPT conversation A

Claude request #2
    ↓
same conversation A

Claude request #3
    ↓
same conversation A
```

This means ChatGPT can maintain conversational context across Claude tool calls.

Future option:

```text
new_chatgpt_chat()
```

or:

```json
{
  "prompt": "...",
  "newChat": true
}
```

Not required for MVP.

---

# 16. Error Handling

The tool should never silently hang.

## Bridge offline

```text
CHATGPT_BRIDGE_OFFLINE:
Chrome extension is not connected.
```

## Bridge disabled

```text
BRIDGE_DISABLED:
The ChatGPT bridge is currently disabled.
```

## No assigned tab

```text
NO_CHATGPT_TAB:
No ChatGPT tab is currently assigned.
```

## Assigned tab closed

```text
ASSIGNED_TAB_CLOSED:
The assigned ChatGPT tab no longer exists.
```

## ChatGPT not ready

```text
CHATGPT_NOT_READY:
The assigned page does not contain a usable ChatGPT composer.
```

## Composer missing

```text
CHATGPT_COMPOSER_NOT_FOUND
```

## Send button missing

```text
CHATGPT_SEND_BUTTON_NOT_FOUND
```

## Timeout

```text
CHATGPT_TIMEOUT:
No completed ChatGPT response was detected before the timeout.
```

## Generation error

```text
CHATGPT_GENERATION_ERROR:
ChatGPT displayed an error while generating the response.
```

## Connection loss

```text
EXTENSION_DISCONNECTED:
The Chrome extension disconnected during the request.
```

Every request must end as one of:

```text
completed
failed
timed_out
cancelled
```

Never leave unresolved futures indefinitely.

---

# 17. Security

The bridge controls a logged-in ChatGPT browser session.

Treat it as privileged.

Bind only to:

```text
127.0.0.1
```

Not:

```text
0.0.0.0
```

unless remote access is deliberately implemented later.

Use an authentication token.

Example:

```text
ws://127.0.0.1:8765/ws?token=<secret>
```

Requirements:

* random high-entropy token
* reject invalid tokens
* reject missing tokens
* validate protocol messages
* maximum prompt size
* maximum response size
* maximum queue size
* request timeout
* bridge enable/disable control

Do not support arbitrary JavaScript execution.

Do not give Claude direct DOM scripting capabilities.

Do not expose arbitrary browser navigation initially.

---

# 18. Filesystem Boundary

ChatGPT should not automatically get filesystem access.

Preferred architecture:

```text
Claude
   ↓
reads repository
   ↓
selects relevant context
   ↓
ask_chatgpt(...)
   ↓
ChatGPT gives advice
   ↓
Claude verifies advice
   ↓
Claude edits/tests
```

Not:

```text
ChatGPT
   ↓
unrestricted local filesystem
```

Claude remains responsible for:

* repository inspection
* selecting context
* making edits
* running tests
* checking call sites
* verifying ChatGPT claims

---

# 19. Extension Safety Switch

The popup should provide:

```text
[ Disable Bridge ]
```

When disabled:

```text
Claude
   ↓
ask_chatgpt(...)
   ↓
BRIDGE_DISABLED
```

The extension only manipulates the explicitly assigned tab.

---

# 20. Logging

Server logs:

```text
[12:14:02] extension connected
[12:14:17] req_123 queued
[12:14:17] req_123 dispatched
[12:14:18] req_123 accepted
[12:14:45] req_123 completed
[12:14:45] duration=28.1s response_chars=5321
```

Extension logs:

```text
bridge connected
assigned tab: 918273
request req_123 received
prompt submitted
assistant response detected
generation complete
response returned
```

Do not log entire prompts or responses by default.

Add optional debug logging if needed.

---

# 21. Manifest V3 Service Worker Handling

The Chrome extension background service worker may stop and restart.

Do not depend exclusively on in-memory state.

Persist where appropriate:

```text
assigned tab ID
bridge enabled/disabled
bridge URL
authentication token
```

On startup:

```text
1. reload stored state
2. reconnect WebSocket
3. validate assigned tab
4. restore safe idle state
```

Do not assume the background worker stays alive forever.

---

# 22. WebSocket Reconnection

If disconnected while idle:

```text
reconnect automatically
```

Use backoff.

For example:

```text
1s
2s
5s
10s
...
```

with a reasonable upper bound.

If disconnected during an active request:

```text
fail active request explicitly
clean pending state
reconnect
allow new requests afterward
```

Do not pretend an in-flight request survived unless a proper recovery mechanism is deliberately implemented.

---

# 23. Development Phases

## Phase 0 — Bootstrap

Create:

* server skeleton
* MCP setup
* WebSocket setup
* Chrome MV3 extension skeleton
* README
* tests directory

Success:

```text
server starts
extension loads
```

No ChatGPT interaction yet.

---

## Phase 1 — Transport

Make:

```text
Claude
 ↓
MCP
 ↓
server
 ↓
extension
 ↓
hardcoded response
 ↓
server
 ↓
Claude
```

work.

Extension response can initially be:

```text
test response
```

Verify:

* request reaches extension
* request ID preserved
* response returns to Claude

Do not proceed until this works reliably.

---

## Phase 2 — Tab Assignment

Add:

```text
Assign Current Tab
Unassign
Open Assigned Tab
Enable/Disable Bridge
```

Verify:

* ChatGPT tab assignment
* wrong page rejection
* closed tab detection
* stale ID cleanup

---

## Phase 3 — Prompt Submission

Make:

```text
ask_chatgpt("Say hello")
```

cause:

```text
Say hello
```

to be submitted in the assigned ChatGPT tab.

Do not yet worry about returning the response.

Test prompt insertion repeatedly.

---

## Phase 4 — Response Capture

Implement:

```text
MutationObserver
assistant response detection
generation state
stabilization
response extraction
```

Now the full flow becomes:

```text
Claude
→ server
→ extension
→ ChatGPT
→ extension
→ server
→ Claude
```

---

## Phase 5 — Reliability

Add:

* request queue
* one-active-request enforcement
* timeout cleanup
* tab close detection
* reconnect handling
* malformed message rejection
* generation error detection
* pending request cleanup
* logs

---

## Phase 6 — Security Hardening

Add:

* authentication token
* localhost-only binding
* queue limits
* request limits
* response limits
* explicit disable control
* protocol validation

---

## Phase 7 — Quality Improvements

After MVP is reliable:

```text
better Markdown extraction
new chat
cancel generation
status tool
attachments
response streaming
multiple worker tabs
conversation selection
```

---

# 24. MVP Acceptance Tests

## MCP

* [ ] `ask_chatgpt` appears in Claude Code.
* [ ] Claude can invoke it.
* [ ] Request reaches local server.
* [ ] Result returns to Claude.

## WebSocket

* [ ] Extension connects.
* [ ] Authentication works.
* [ ] Reconnection works.
* [ ] Request IDs match.
* [ ] Invalid protocol messages are rejected.

## Tab Assignment

* [ ] Current ChatGPT tab can be assigned.
* [ ] Tab can be unassigned.
* [ ] Wrong page is rejected.
* [ ] Closed tab is detected.

## ChatGPT Interaction

* [ ] Composer is detected.
* [ ] Prompt is inserted.
* [ ] Prompt is submitted.
* [ ] New assistant response is detected.
* [ ] Generation completion is detected.
* [ ] Response text is extracted.
* [ ] Correct response returns to correct request.

## Queueing

* [ ] One request active.
* [ ] Additional requests queue.
* [ ] Queue continues after successful response.
* [ ] Queue continues after failed response.
* [ ] Queue continues after timeout.

## Errors

* [ ] extension offline
* [ ] bridge disabled
* [ ] no assigned tab
* [ ] assigned tab closed
* [ ] composer missing
* [ ] send button missing
* [ ] generation error
* [ ] response timeout
* [ ] WebSocket disconnect

## Security

* [ ] bound only to `127.0.0.1`
* [ ] authentication required
* [ ] no arbitrary JS
* [ ] no unrestricted filesystem exposure
* [ ] only assigned tab controlled

---

# 25. Recommended End-to-End Test

Use a deterministic request:

```text
ask_chatgpt(
    "Respond with exactly: bridge-test-ok"
)
```

Expected flow:

```text
Claude invokes tool
      ↓
local server receives request
      ↓
extension receives request
      ↓
assigned ChatGPT tab submits prompt
      ↓
ChatGPT answers
      ↓
extension captures answer
      ↓
local server resolves request
      ↓
Claude receives:
bridge-test-ok
```

Repeat the test multiple times.

Then send another prompt into the same assigned conversation to verify persistent conversation behavior.

---

# 26. Suggested Claude Project Instruction

Once the bridge works, Claude can be told:

```markdown
## ChatGPT Consultation

The MCP tool `ask_chatgpt` is available.

Use it when:
- the user explicitly asks you to consult ChatGPT;
- an independent second opinion would materially improve confidence;
- you are uncertain about a difficult design decision;
- an independent code review would help identify missed issues.

When consulting ChatGPT:

1. Include enough relevant context.
2. Ask a precise question.
3. Do not assume ChatGPT can access the local repository.
4. Treat the response as advice, not source of truth.
5. Verify its claims against the current filesystem.
6. Do not outsource verification to ChatGPT.
```

---

# 27. Future Features

Only after the core bridge is reliable:

```text
get_chatgpt_status()
new_chatgpt_chat()
cancel_chatgpt_request()
ask_chatgpt_with_files()
stream_chatgpt_response()
```

Potential multi-tab system:

```text
Claude
   ↓
request manager
   ├── ChatGPT worker 1
   ├── ChatGPT worker 2
   └── ChatGPT worker 3
```

Potential explicit attachment support:

```text
Claude chooses files
   ↓
bridge validates explicit list
   ↓
extension uploads only those files
   ↓
ChatGPT receives files
```

Never expose arbitrary local files.

---

# 28. Core Design Decisions

Keep these unless a real technical constraint requires changing them:

1. Claude integration uses MCP.
2. MCP preferably uses stdio.
3. Browser bridge uses WebSocket.
4. Chrome extension initiates WebSocket connection.
5. Server binds to `127.0.0.1`.
6. User explicitly assigns ChatGPT tab.
7. Extension only controls assigned tab.
8. One active request per tab.
9. Every request has a unique ID.
10. Response detection is event-driven.
11. MVP extracts plain text.
12. ChatGPT has no automatic filesystem access.
13. Claude verifies ChatGPT advice.
14. Transport is built before browser automation.
15. Reliability comes before features.

---

# 29. Definition of Done

The first useful version is done when this exact workflow works reliably:

```text
1. Start the local bridge server.

2. Start/load Chrome extension.

3. Extension connects to localhost.

4. Open ChatGPT.

5. Assign that ChatGPT tab.

6. Claude Code calls:

   ask_chatgpt("Review this implementation.")

7. Server receives the request.

8. Server sends it to extension.

9. Extension submits prompt into assigned ChatGPT tab.

10. ChatGPT generates its response.

11. Extension detects completion.

12. Extension extracts response.

13. Response returns with matching request ID.

14. MCP call resolves.

15. Claude receives the answer in the console.

16. Claude independently verifies the answer before acting on it.
```

That is the MVP.
