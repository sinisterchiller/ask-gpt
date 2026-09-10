"""Data models for the ChatGPT Bridge protocol."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RequestState(str, Enum):
    QUEUED = "queued"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ErrorCode(str, Enum):
    CHATGPT_BRIDGE_OFFLINE = "CHATGPT_BRIDGE_OFFLINE"
    BRIDGE_DISABLED = "BRIDGE_DISABLED"
    NO_CHATGPT_TAB = "NO_CHATGPT_TAB"
    ASSIGNED_TAB_CLOSED = "ASSIGNED_TAB_CLOSED"
    CHATGPT_NOT_READY = "CHATGPT_NOT_READY"
    CHATGPT_COMPOSER_NOT_FOUND = "CHATGPT_COMPOSER_NOT_FOUND"
    CHATGPT_SEND_BUTTON_NOT_FOUND = "CHATGPT_SEND_BUTTON_NOT_FOUND"
    CHATGPT_TIMEOUT = "CHATGPT_TIMEOUT"
    CHATGPT_GENERATION_ERROR = "CHATGPT_GENERATION_ERROR"
    EXTENSION_DISCONNECTED = "EXTENSION_DISCONNECTED"
    INVALID_PROTOCOL_MESSAGE = "INVALID_PROTOCOL_MESSAGE"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    QUEUE_FULL = "QUEUE_FULL"


@dataclass
class BridgeRequest:
    """Tracks one end-to-end request."""

    request_id: str
    prompt: str
    timeout_ms: int
    created_at: float = field(default_factory=time.time)
    state: RequestState = RequestState.QUEUED
    future: Any = field(default=None, repr=False)
    response: str = ""
    error_code: str = ""
    error_message: str = ""

    def is_expired(self) -> bool:
        elapsed_ms = (time.time() - self.created_at) * 1000
        return elapsed_ms >= self.timeout_ms


@dataclass
class ServerState:
    """Mutable shared state for the bridge server."""

    extension_connected: bool = False
    bridge_enabled: bool = True
    assigned_tab_id: int | None = None
    assigned_tab_url: str = ""
    pending_requests: dict[str, BridgeRequest] = field(default_factory=dict)
    queue: list[BridgeRequest] = field(default_factory=list)
    active_request: BridgeRequest | None = None
    ws_token: str = ""  # set at startup from config
