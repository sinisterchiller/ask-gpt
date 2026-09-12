"""Configuration for the ChatGPT Bridge server."""

import os
import secrets


# Network
HOST = "127.0.0.1"
PORT = 8765

# WebSocket
WS_PATH = "/ws"

# Authentication token — generated once, persisted to disk
_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", ".bridge_token")


def get_token() -> str:
    """Return a stable authentication token, creating one if none exists."""
    token_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", ".bridge_token"
    )
    if os.path.exists(token_path):
        with open(token_path, "r") as f:
            token = f.read().strip()
            if token:
                return token
    token = secrets.token_hex(32)
    with open(token_path, "w") as f:
        f.write(token)
    return token


# Limits
MAX_PROMPT_SIZE = None  # no limit
MAX_RESPONSE_SIZE = None  # no limit
MAX_QUEUE_SIZE = None  # no limit
REQUEST_TIMEOUT_MS = 300_000  # 5 minutes default

# Extension connection readiness timeout (seconds)
# How long to wait for the extension to connect on first invocation.
# The extension normally reconnects within 1-2 seconds of the server starting.
EXTENSION_READY_TIMEOUT = 5.0


# Logging
LOG_PREFIX = "[chatgpt-bridge]"
