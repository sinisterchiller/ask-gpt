# Agent Instructions — Claude Code ↔ ChatGPT Browser Bridge

You are implementing a local bridge that allows Claude Code to call an MCP tool, send a prompt through a localhost server to a Chrome extension, submit the prompt into a user-assigned ChatGPT browser tab, capture ChatGPT's completed response, and return that response to Claude Code.

Treat the current filesystem, current project state, and latest user instructions as the source of truth.

Optimize for:

```text
correctness
completeness
verification
reliability
minimal assumptions
```

Do not optimize for finishing quickly.

---

# 1. Primary Goal

Build this workflow:

```text
Claude Code
    ↓
MCP tool: ask_chatgpt(prompt)
    ↓
localhost bridge
    ↓
WebSocket
    ↓
Chrome extension
    ↓
explicitly assigned ChatGPT tab
    ↓
ChatGPT response
    ↓
Chrome extension
    ↓
localhost bridge
    ↓
MCP result
    ↓
Claude Code
```

The finished implementation must return the actual completed ChatGPT response to the original Claude Code tool call.

---

# 2. Inspect Before Claiming

Before making a factual claim about the implementation:

1. locate the relevant source
2. read the current contents
3. inspect relevant call sites
4. verify actual behavior
5. then state the claim

Do not rely on:

* memory
* previous file contents
* filenames
* summaries
* assumptions
* old agent output
* earlier tests when the implementation has since changed

If the user tells you something changed, assume previously read files may now be stale.

Re-read them.

---

# 3. Mandatory Architecture

Unless a verified technical constraint requires changing it, use:

```text
Claude Code
    ↓ MCP over stdio
Local Python server
    ↓ WebSocket
Chrome MV3 extension
    ↓ content script
Assigned ChatGPT tab
```

The extension initiates the WebSocket connection.

The server listens on:

```text
127.0.0.1
```

Default WebSocket:

```text
ws://127.0.0.1:8765/ws
```

Do not expose the bridge on:

```text
0.0.0.0
```

without explicit user instruction.

---

# 4. MVP Scope

Implement only:

* one Claude Code client
* one local MCP server
* one Chrome extension
* one assigned ChatGPT tab
* one active request
* queued additional requests
* text prompt
* text response
* persistent ChatGPT conversation

Do not prematurely implement:

```text
multiple tabs
parallel ChatGPT workers
attachments
images
streaming
new chat
automatic filesystem access
arbitrary browser automation
arbitrary JavaScript execution
perfect Markdown extraction
```

---

# 5. MCP Interface

Initial tool:

```text
ask_chatgpt
```

Recommended interface:

```text
ask_chatgpt(
    prompt: string,
    timeout?: integer
)
```

The tool must wait for the browser response.

Do not make the tool return merely because the prompt was submitted.

Successful completion means:

```text
ChatGPT completed response
→ extension captured it
→ local server received it
→ original MCP call received it
```

---

# 6. Every Request Requires an ID

Generate a unique request ID before sending anything to the browser.

Example:

```json
{
  "type": "prompt",
  "requestId": "req_123",
  "prompt": "Review this code.",
  "timeout": 180000
}
```

All subsequent messages concerning the request must include the same ID.

Do not infer which response belongs to which request based on ordering alone.

---

# 7. Track Pending Requests Explicitly

Use an explicit request manager.

Conceptually:

```python
pending_requests = {
    "req_123": future
}
```

Possible states:

```text
queued
active
completed
failed
timed_out
cancelled
```

Every request must eventually leave the pending state.

No unresolved future may remain indefinitely.

---

# 8. Build Incrementally

Do not attempt the full implementation in one pass.

Follow the phases in order.

Do not skip verification between phases.

---

# 9. Phase 0 — Bootstrap

Create or inspect project structure.

Suggested structure:

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
├── README.md
├── requirements.txt
└── .gitignore
```

Do not restructure an existing working repository unnecessarily.

---

# 10. Phase 1 — Transport Only

Before interacting with the ChatGPT DOM, make this work:

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

For example:

```text
ask_chatgpt("hello")
```

may initially cause the extension to return:

```text
test response
```

Verify:

* Claude can see MCP tool
* Claude can call MCP tool
* server receives request
* extension receives request
* request ID survives round trip
* extension response reaches server
* MCP call resolves

Do not continue to Phase 2 until this is verified.

---

# 11. Phase 2 — Tab Assignment

Implement explicit manual assignment.

Required extension actions:

```text
Assign Current Tab
Unassign
Open Assigned Tab
Enable Bridge
Disable Bridge
```

Never automatically choose a ChatGPT tab.

Never automatically attach to all ChatGPT tabs.

Validate:

* assigned tab exists
* page belongs to ChatGPT
* content script can communicate
* stale tab IDs are cleared
* closed tab is detected

---

# 12. Phase 3 — Prompt Submission

Only after transport works, implement ChatGPT DOM interaction.

The content script must:

```text
1. verify ChatGPT page is ready
2. locate composer
3. focus composer
4. insert exact prompt
5. dispatch required frontend events
6. find send control
7. submit
8. return success/error
```

Do not rely mainly on generated CSS classes.

Prefer:

```text
role
aria-label
contenteditable
stable data attributes
button state
semantic DOM relationships
```

Keep selectors in one module.

If selectors stop working, inspect the current ChatGPT DOM before editing them.

Do not guess.

---

# 13. Do Not Use Fixed Sleeps

Do not implement response capture as:

```text
submit
sleep
read last message
```

Use:

```javascript
MutationObserver
```

and generation-state observation.

Small stabilization delays are acceptable after event-driven completion detection.

---

# 14. Phase 4 — Response Capture

Recommended state machine:

```text
IDLE
SUBMITTED
WAITING_FOR_ASSISTANT
GENERATING
STABILIZING
COMPLETE
ERROR
```

Procedure:

```text
1. Record current assistant-message state.

2. Submit prompt.

3. Detect new assistant response.

4. Observe its DOM mutations.

5. Track changes in response text.

6. Observe ChatGPT generation state.

7. Detect ChatGPT errors.

8. Detect generation end.

9. Wait briefly for text stabilization.

10. Extract final response.

11. Send it back with request ID.
```

Use multiple completion signals.

Do not depend on one fragile element.

---

# 15. Response Extraction

For MVP use:

```javascript
assistantElement.innerText
```

Reliability is more important than perfect Markdown initially.

Do not spend significant time building Markdown reconstruction before the end-to-end bridge works.

---

# 16. Queueing

Only one request may actively control one assigned ChatGPT conversation.

```text
MAX_CONCURRENT_CHATGPT_REQUESTS = 1
```

If requests arrive:

```text
A → active
B → queued
C → queued
```

After A finishes:

```text
B → active
C → queued
```

Ensure failures and timeouts also release the queue.

---

# 17. Required Errors

Implement clear structured errors.

At minimum:

```text
CHATGPT_BRIDGE_OFFLINE
BRIDGE_DISABLED
NO_CHATGPT_TAB
ASSIGNED_TAB_CLOSED
CHATGPT_NOT_READY
CHATGPT_COMPOSER_NOT_FOUND
CHATGPT_SEND_BUTTON_NOT_FOUND
CHATGPT_TIMEOUT
CHATGPT_GENERATION_ERROR
EXTENSION_DISCONNECTED
INVALID_PROTOCOL_MESSAGE
AUTHENTICATION_FAILED
```

Do not allow silent hangs.

---

# 18. Security Requirements

The bridge controls a logged-in browser session.

Mandatory:

```text
bind only to 127.0.0.1
authentication token
protocol validation
prompt size limit
response size limit
queue size limit
request timeout
manual bridge disable
explicit tab assignment
```

Do not:

```text
allow arbitrary JavaScript execution
provide arbitrary filesystem access
allow arbitrary URL navigation
control unassigned tabs
expose the server to the LAN by default
```

---

# 19. WebSocket Authentication

Require a random secret.

Example:

```text
ws://127.0.0.1:8765/ws?token=<secret>
```

Reject:

* missing token
* incorrect token
* malformed messages
* unsupported protocol versions

Do not log the token.

---

# 20. Manifest V3 Service Worker

Chrome Manifest V3 background workers may stop.

Do not assume they remain alive.

Persist important state:

```text
assigned tab ID
bridge enabled state
bridge URL
authentication token
```

When the service worker starts:

```text
reload state
validate assignment
reconnect WebSocket
return to safe idle state
```

---

# 21. Reconnection

If WebSocket disconnects while idle:

```text
reconnect with backoff
```

If it disconnects during an active request:

```text
fail request explicitly
clean pending request
release queue
reconnect
```

Do not silently assume the request survived.

---

# 22. Filesystem Boundary

Do not give ChatGPT unrestricted filesystem access.

Claude remains responsible for:

```text
reading files
reading call sites
selecting context
sending relevant information
editing project
running tests
verifying ChatGPT output
```

Flow:

```text
Claude reads current source
      ↓
Claude sends relevant context to ChatGPT
      ↓
ChatGPT gives second opinion
      ↓
Claude verifies against source
      ↓
Claude decides whether to act
```

ChatGPT output is advice.

It is not source of truth.

---

# 23. Never Assume ChatGPT Advice Is Correct

When ChatGPT responds:

1. read the response
2. identify its claims
3. inspect the actual relevant files
4. test applicable claims
5. then decide whether to modify anything

Do not blindly apply a ChatGPT recommendation.

---

# 24. Logging

Add enough logs to diagnose the bridge.

Server:

```text
extension connected
request queued
request dispatched
request accepted
response received
request completed
request timed out
extension disconnected
```

Extension:

```text
server connected
tab assigned
request received
composer found
prompt inserted
prompt submitted
assistant detected
generation complete
response returned
```

Do not log full prompts or responses by default.

---

# 25. Testing

Test layers independently.

## MCP

Test:

* tool registration
* normal request
* bad input
* timeout
* error propagation

## WebSocket

Test:

* valid authentication
* invalid authentication
* connect
* disconnect
* reconnect
* malformed protocol
* correct request matching

## Request manager

Test:

* FIFO queue
* one active request
* success cleanup
* timeout cleanup
* failure cleanup
* queue continues after failure

## Extension

Test:

* assign
* unassign
* closed tab
* wrong page
* service-worker restart
* WebSocket reconnect

## ChatGPT automation

Test:

* short prompt
* long prompt
* multi-paragraph prompt
* normal response
* long response
* code block response
* ChatGPT error
* response timeout

---

# 26. Verify Before Reporting Success

Do not report a phase as complete because:

```text
files exist
code compiles
extension loads
function was implemented
no syntax error occurred
```

Verify the actual intended behavior.

For example:

Phase 1 is only complete when:

```text
actual Claude MCP call
→ actual server
→ actual extension
→ actual response
→ actual Claude tool result
```

has been demonstrated.

---

# 27. Inspect Diffs

After edits:

1. inspect changed files
2. inspect diff
3. ensure no accidental unrelated changes
4. run applicable tests
5. verify behavior

Do not perform broad refactors unless needed for correctness.

---

# 28. Minimal Changes

When fixing a bug:

```text
identify root cause
find affected call sites
make smallest correct change
test
inspect diff
verify
```

Do not rewrite working systems simply because another architecture appears cleaner.

---

# 29. Do Not Spawn Subagents

Do the work yourself.

Do not spawn secondary agents unless the user explicitly requests it.

The task benefits from one agent retaining full context across:

```text
server
MCP
protocol
extension
DOM automation
tests
```

---

# 30. Do Not Stop Early

If asked to implement a phase, continue until that phase is actually complete and tested.

Do not stop at:

```text
scaffolding
TODO comments
placeholder functions
unverified code
```

unless a genuine external blocker prevents continuing.

If blocked, clearly state:

```text
VERIFIED:
...

UNVERIFIED:
...

BLOCKER:
...
```

---

# 31. ChatGPT DOM Changes

ChatGPT's frontend may change.

Therefore:

```text
isolate selectors
avoid deep generated CSS paths
prefer semantic attributes
separate prompt submission from response detection
return explicit selector errors
```

If behavior breaks:

```text
inspect current DOM
identify actual change
update smallest affected selector/logic
test prompt submission
test response capture
```

Do not blindly add fallback selectors until something happens to work.

---

# 32. Browser State Validation

Before every request verify:

```text
bridge enabled
extension connected
assigned tab exists
assigned tab is ChatGPT
content script is responsive
no conflicting active request
```

Fail early with a structured error where possible.

---

# 33. Protocol Validation

Never trust inbound WebSocket JSON blindly.

Validate:

```text
type
requestId
prompt type
timeout
message size
protocol version
```

Reject unknown message types cleanly.

---

# 34. User Control

The user must remain in control of which ChatGPT conversation Claude can use.

Required behavior:

```text
user opens desired ChatGPT conversation
user clicks Assign Current Tab
extension stores exact tab
Claude requests go only there
```

No automatic conversation switching in MVP.

---

# 35. Initial End-to-End Verification

Use:

```text
Respond with exactly: bridge-test-ok
```

Expected result:

```text
ask_chatgpt(...)
        ↓
ChatGPT tab
        ↓
bridge-test-ok
        ↓
Claude Code tool result
```

Repeat this test multiple times.

Then send a second request to the same conversation.

---

# 36. Acceptance Checklist

Do not call the MVP complete until these are verified.

## MCP

* [ ] tool exists
* [ ] Claude sees tool
* [ ] Claude calls tool
* [ ] tool waits for response
* [ ] tool returns ChatGPT output

## Server

* [ ] listens on 127.0.0.1
* [ ] authentication required
* [ ] request IDs generated
* [ ] pending requests tracked
* [ ] queue works
* [ ] timeouts work

## Extension

* [ ] connects to server
* [ ] reconnects
* [ ] tab can be assigned
* [ ] tab can be unassigned
* [ ] stale tab detected
* [ ] bridge can be disabled

## ChatGPT Interaction

* [ ] composer found
* [ ] exact prompt inserted
* [ ] prompt submitted
* [ ] assistant response detected
* [ ] completion detected
* [ ] final text extracted
* [ ] correct response returned

## Errors

* [ ] extension offline
* [ ] bridge disabled
* [ ] no tab
* [ ] closed tab
* [ ] wrong page
* [ ] missing composer
* [ ] missing send control
* [ ] timeout
* [ ] ChatGPT error
* [ ] WebSocket disconnect

## Security

* [ ] localhost only
* [ ] token required
* [ ] no arbitrary JS
* [ ] no filesystem exposure
* [ ] only assigned tab controlled

---

# 37. Future Features

Do not implement until the MVP is reliable:

```text
get_chatgpt_status()
new_chatgpt_chat()
cancel_chatgpt_request()
ask_chatgpt_with_files()
stream_chatgpt_response()
multiple ChatGPT tabs
parallel ChatGPT workers
Markdown reconstruction
attachments
images
conversation manager
request history
```

---

# 38. Core Principle

The goal is not to create a demo that works once.

The goal is a dependable tool where:

```text
Claude makes a tool call
        ↓
exact prompt reaches assigned ChatGPT conversation
        ↓
ChatGPT generates response
        ↓
correct completed response returns to Claude
        ↓
failures are explicit
        ↓
nothing silently hangs
        ↓
Claude verifies the result before using it
```

Build that path first.

Verify it thoroughly.

Only then add features.
