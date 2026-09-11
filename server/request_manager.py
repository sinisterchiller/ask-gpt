"""Request manager — queues, activates, and cleans up bridge requests."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import MAX_QUEUE_SIZE, REQUEST_TIMEOUT_MS
from .models import BridgeRequest, RequestState
from .protocol import generate_request_id, make_error

logger = logging.getLogger("chatgpt-bridge.request_manager")


class RequestManager:
    """Manages the lifecycle of bridge requests.

    - Assigns unique IDs
    - Queues requests when a request is already active
    - Activates the next queued request when the current one finishes
    - Cleans up timeouts and failures
    """

    def __init__(self, on_request_complete: Any | None = None) -> None:
        self._requests: dict[str, BridgeRequest] = {}
        self._queue: list[BridgeRequest] = []
        self._active: BridgeRequest | None = None
        self._on_request_complete = on_request_complete

    # --- Public API ---

    def create_request(self, prompt: str, timeout_ms: int | None = None) -> BridgeRequest:
        """Create a new queued request."""
        if timeout_ms is None:
            timeout_ms = REQUEST_TIMEOUT_MS

        request = BridgeRequest(
            request_id=generate_request_id(),
            prompt=prompt,
            timeout_ms=timeout_ms,
        )
        self._requests[request.request_id] = request
        self._queue.append(request)
        logger.info("[REQ %s] queued (queue_size=%d)", request.request_id, len(self._queue))

        # Auto-activate if nothing is active
        self._try_activate_next()

        return request

    @property
    def queue(self) -> list[BridgeRequest]:
        return list(self._queue)

    @property
    def active(self) -> BridgeRequest | None:
        return self._active

    @property
    def is_full(self) -> bool:
        return len(self._queue) >= MAX_QUEUE_SIZE

    def activate_next(self) -> BridgeRequest | None:
        """Move the next queued request to active. Returns None if queue is empty."""
        if self._active is not None:
            logger.debug("Cannot activate: request %s is still active", self._active.request_id)
            return None

        if not self._queue:
            return None

        request = self._queue.pop(0)
        request.state = RequestState.ACTIVE
        self._active = request
        logger.info("[REQ %s] activated", request.request_id)
        return request

    def complete_request(self, request_id: str, response: str = "", error_code: str = "") -> None:
        """Mark a request as completed or failed, then activate the next queued request."""
        logger.info("[REQ_MGR] complete_request START(%s, response=%d chars, error=%s), active=%s, queue_size=%d",
                    request_id, len(response), error_code,
                    self._active.request_id if self._active else "None",
                    len(self._queue))
        request = self._requests.get(request_id)
        if request is None:
            logger.warning("Completed unknown request: %s", request_id)
            return

        if response:
            request.state = RequestState.COMPLETED
            request.response = response
        elif error_code:
            request.state = RequestState.FAILED
            request.error_code = error_code
        logger.info(
            "[REQ %s] %s (response_chars=%d)",
            request_id,
            request.state.value,
            len(request.response),
        )

        # Clear active reference
        if self._active and self._active.request_id == request_id:
            logger.info("[REQ_MGR] Clearing active reference for %s", request_id)
            self._active = None

        # Notify callback
        if self._on_request_complete:
            logger.info("[REQ_MGR] Calling _on_request_complete callback for %s", request_id)
            self._on_request_complete(request_id)

        # Try to activate next
        self._try_activate_next()

    def fail_request(self, request_id: str, error_code: str, error_message: str = "") -> None:
        """Fail a request with an error code."""
        self.complete_request(request_id, error_code=error_code)

    def cancel_request(self, request_id: str) -> None:
        """Cancel a queued request."""
        request = self._requests.get(request_id)
        if request is None:
            return
        if request.state == RequestState.QUEUED:
            request.state = RequestState.CANCELLED
            self._queue = [r for r in self._queue if r.request_id != request_id]
            logger.info("[REQ %s] cancelled", request_id)
            # If nothing is active, try to activate next
            if self._active is None:
                self._try_activate_next()
        elif request.state == RequestState.ACTIVE:
            request.state = RequestState.CANCELLED
            if self._active and self._active.request_id == request_id:
                self._active = None
            logger.info("[REQ %s] cancelled (was active)", request_id)
            self._try_activate_next()

    def get_request(self, request_id: str) -> BridgeRequest | None:
        return self._requests.get(request_id)

    def cleanup_expired(self) -> list[str]:
        """Find and clean up expired requests. Returns list of cleaned request IDs."""
        expired_ids = []
        now = time.time()

        # Check active request
        if self._active and self._active.is_expired():
            expired_ids.append(self._active.request_id)
            self._active.state = RequestState.TIMED_OUT
            logger.warning("[REQ %s] timed out (active)", self._active.request_id)
            self._active = None

        # Check queued requests
        remaining = []
        for req in self._queue:
            if req.is_expired():
                req.state = RequestState.TIMED_OUT
                expired_ids.append(req.request_id)
                logger.warning("[REQ %s] timed out (queued)", req.request_id)
            else:
                remaining.append(req)
        self._queue = remaining

        # Try to activate next if active was cleared
        if self._active is None and self._queue:
            self._try_activate_next()

        return expired_ids

    def get_status(self) -> dict[str, Any]:
        """Return current request manager status."""
        return {
            "active": self._active.request_id if self._active else None,
            "queued": len(self._queue),
            "queue_ids": [r.request_id for r in self._queue],
            "max_queue_size": MAX_QUEUE_SIZE,
        }

    # --- Private ---

    def _try_activate_next(self) -> None:
        if self._active is None and self._queue:
            self.activate_next()
