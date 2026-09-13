"""Tests for the request manager."""

import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server.request_manager import RequestManager
from server.models import RequestState


class TestCreateRequest:
    def test_creates_queued_request(self):
        rm = RequestManager()
        req = rm.create_request("Hello")
        assert req.prompt == "Hello"
        assert req.request_id in rm._requests

    def test_custom_timeout(self):
        rm = RequestManager()
        req = rm.create_request("Hello", timeout_ms=60000)
        assert req.timeout_ms == 60000

    def test_default_timeout(self):
        rm = RequestManager()
        req = rm.create_request("Hello")
        assert req.timeout_ms == 1800000  # REQUEST_TIMEOUT_MS (30 minutes)

    def test_auto_activates_first(self):
        rm = RequestManager()
        r1 = rm.create_request("First")
        assert rm.active is r1
        assert r1.state == RequestState.ACTIVE

    def test_second_request_queued(self):
        rm = RequestManager()
        rm.create_request("First")
        r2 = rm.create_request("Second")
        assert rm.active is not None
        assert r2.state == RequestState.QUEUED
        assert r2 in rm.queue


class TestQueue:
    def test_queue_order(self):
        rm = RequestManager()
        rm.create_request("First")  # auto-activated
        r2 = rm.create_request("Second")
        r3 = rm.create_request("Third")

        assert rm.queue[0] is r2
        assert rm.queue[1] is r3

    def test_complete_activates_next(self):
        rm = RequestManager()
        r1 = rm.create_request("First")  # auto-activated
        r2 = rm.create_request("Second")

        rm.complete_request(r1.request_id, response="done")

        assert rm.active is r2
        assert r2.state == RequestState.ACTIVE
        assert r2 not in rm.queue

    def test_no_activate_when_active(self):
        rm = RequestManager()
        r1 = rm.create_request("First")  # auto-activated

        # Should not activate another while first is active
        result = rm.activate_next()
        assert result is None

    def test_fail_activates_next(self):
        rm = RequestManager()
        r1 = rm.create_request("First")  # auto-activated
        r2 = rm.create_request("Second")

        rm.complete_request(r1.request_id, error_code="ERROR_X")

        assert rm.active is r2
        assert r2.state == RequestState.ACTIVE

    def test_empty_queue_returns_none(self):
        rm = RequestManager()
        assert rm.activate_next() is None


class TestCancel:
    def test_cancel_queued(self):
        rm = RequestManager()
        r1 = rm.create_request("First")  # auto-activated
        r2 = rm.create_request("Second")  # queued
        rm.cancel_request(r2.request_id)

        assert r2.state == RequestState.CANCELLED
        assert len(rm.queue) == 0

    def test_cancel_active_releases(self):
        rm = RequestManager()
        r1 = rm.create_request("First")  # auto-activated
        r2 = rm.create_request("Second")  # queued

        rm.cancel_request(r1.request_id)

        assert r1.state == RequestState.CANCELLED
        assert rm.active is r2  # r2 gets activated
        assert r2.state == RequestState.ACTIVE


class TestTimeout:
    def test_expired_active(self):
        rm = RequestManager()
        req = rm.create_request("Test", timeout_ms=100)  # 100ms timeout
        # Wait for it to expire
        time.sleep(0.2)

        expired = rm.cleanup_expired()
        assert req.request_id in expired
        assert req.state == RequestState.TIMED_OUT
        assert rm._active is None

    def test_expired_queued(self):
        rm = RequestManager()
        rm.create_request("First", timeout_ms=100)  # auto-activated, will expire
        time.sleep(0.2)

        r2 = rm.create_request("Second", timeout_ms=100)  # queued, will expire
        time.sleep(0.2)

        expired = rm.cleanup_expired()
        assert len(expired) == 2
        assert rm._active is None
        assert len(rm.queue) == 0

    def test_non_expired_not_cleaned(self):
        rm = RequestManager()
        rm.create_request("Test", timeout_ms=60000)  # 60s timeout

        expired = rm.cleanup_expired()
        assert expired == []
        assert len(rm.queue) == 0  # queue is empty because it was auto-activated
        assert rm._active is not None  # but it's active


class TestQueueFull:
    def test_max_queue_size(self):
        rm = RequestManager()
        rm.create_request("First")  # auto-activated, removed from queue
        for i in range(10):
            rm.create_request(f"Request {i}")

        assert rm.is_full is True  # 10 items in queue

    def test_not_full_when_under(self):
        rm = RequestManager()
        rm.create_request("Test")
        assert rm.is_full is False


class TestStatus:
    def test_get_status(self):
        rm = RequestManager()
        rm.create_request("First")  # auto-activated
        rm.create_request("Second")

        status = rm.get_status()
        assert status["queued"] == 1
        assert status["active"] is not None
        assert status["max_queue_size"] == 10


class TestGetRequest:
    def test_find_by_id(self):
        rm = RequestManager()
        req = rm.create_request("Test")
        found = rm.get_request(req.request_id)
        assert found is req

    def test_missing_id(self):
        rm = RequestManager()
        rm.create_request("Test")
        assert rm.get_request("nonexistent") is None
