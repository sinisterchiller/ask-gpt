"""WebSocket protocol message types and helpers."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .config import MAX_PROMPT_SIZE, MAX_RESPONSE_SIZE
from .models import ErrorCode


# --- Message type constants ---
MSG_HELLO = "hello"
MSG_HELLO_ACK = "hello_ack"
MSG_PROMPT = "prompt"
MSG_ACCEPTED = "accepted"
MSG_RESPONSE = "response"
MSG_ERROR = "error"
MSG_TAB_CHANGED = "tab_changed"
MSG_BRIDGE_STATE = "bridge_state"
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_DIAGNOSTIC = "diagnostic"

VALID_TYPES = {
    MSG_HELLO,
    MSG_HELLO_ACK,
    MSG_PROMPT,
    MSG_ACCEPTED,
    MSG_RESPONSE,
    MSG_ERROR,
    MSG_TAB_CHANGED,
    MSG_BRIDGE_STATE,
    MSG_PING,
    MSG_PONG,
    MSG_DIAGNOSTIC,
}

VALID_EXTENSION_HELLO_TYPES = {MSG_HELLO}
VALID_SERVER_HELLO_ACK_TYPES = {MSG_HELLO_ACK}
VALID_PROMPT_FIELDS = {"type", "requestId", "prompt", "timeout"}
VALID_RESPONSE_FIELDS = {"type", "requestId", "response", "url"}
VALID_ERROR_FIELDS = {"type", "requestId", "code", "message"}


def generate_request_id() -> str:
    """Generate a unique request ID."""
    return f"req_{uuid.uuid4().hex[:8]}"


def parse_message(raw: str) -> dict[str, Any] | None:
    """Parse a raw WebSocket message string into a dict.

    Returns None if the message is not valid JSON.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def validate_hello(message: dict[str, Any]) -> bool:
    """Validate an extension hello message."""
    if not isinstance(message, dict):
        return False
    if message.get("type") not in VALID_EXTENSION_HELLO_TYPES:
        return False
    return True


def validate_prompt(message: dict[str, Any]) -> bool:
    """Validate a prompt message from the MCP server."""
    if not isinstance(message, dict):
        return False
    if message.get("type") != MSG_PROMPT:
        return False
    if "requestId" not in message or not isinstance(message["requestId"], str):
        return False
    if "prompt" not in message or not isinstance(message["prompt"], str):
        return False
    if MAX_PROMPT_SIZE is not None and len(message["prompt"]) > MAX_PROMPT_SIZE:
        return False
    # Extra fields are tolerated but requestId and prompt are required
    return True


def validate_response(message: dict[str, Any]) -> bool:
    """Validate a response message from the extension."""
    if not isinstance(message, dict):
        return False
    if message.get("type") != MSG_RESPONSE:
        return False
    if "requestId" not in message or not isinstance(message["requestId"], str):
        return False
    if "response" not in message or not isinstance(message["response"], str):
        return False
    if MAX_RESPONSE_SIZE is not None and len(message["response"]) > MAX_RESPONSE_SIZE:
        return False
    return True


def make_hello_ack() -> dict[str, Any]:
    """Server's hello acknowledgment."""
    return {
        "type": MSG_HELLO_ACK,
        "protocolVersion": 1,
    }


def make_prompt(
    request_id: str, prompt: str, timeout: int = 300_000
) -> dict[str, Any]:
    """Create a prompt message for the extension."""
    return {
        "type": MSG_PROMPT,
        "requestId": request_id,
        "prompt": prompt,
        "timeout": timeout,
    }


def make_accepted(request_id: str) -> dict[str, Any]:
    """Acknowledge that a request was accepted by the extension."""
    return {
        "type": MSG_ACCEPTED,
        "requestId": request_id,
    }


def make_response(request_id: str, response: str, url: str = "") -> dict[str, Any]:
    """Create a response message from the extension."""
    msg: dict[str, Any] = {
        "type": MSG_RESPONSE,
        "requestId": request_id,
        "response": response,
    }
    if url:
        msg["url"] = url
    return msg


def make_error(
    request_id: str | None,
    code: ErrorCode | str,
    message: str,
) -> dict[str, Any]:
    """Create an error message."""
    msg: dict[str, Any] = {
        "type": MSG_ERROR,
        "code": code.value if isinstance(code, ErrorCode) else code,
        "message": message,
    }
    if request_id is not None:
        msg["requestId"] = request_id
    return msg


def make_tab_changed(
    tab_id: int | None, url: str = ""
) -> dict[str, Any]:
    """Notify server of a tab assignment change."""
    msg: dict[str, Any] = {
        "type": MSG_TAB_CHANGED,
        "tabId": tab_id,
    }
    if url:
        msg["url"] = url
    return msg


def make_bridge_state(enabled: bool) -> dict[str, Any]:
    """Notify extension of bridge state change."""
    return {
        "type": MSG_BRIDGE_STATE,
        "enabled": enabled,
    }


def make_ping() -> dict[str, Any]:
    return {"type": MSG_PING}


def make_pong() -> dict[str, Any]:
    return {"type": MSG_PONG}


def encode(message: dict[str, Any]) -> str:
    """Serialize a message dict to JSON."""
    return json.dumps(message)
