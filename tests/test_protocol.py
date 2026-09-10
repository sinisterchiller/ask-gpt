"""Tests for the protocol module."""

import sys
import os

# Allow importing from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import protocol
from server.models import ErrorCode
from server.config import MAX_PROMPT_SIZE, MAX_RESPONSE_SIZE


class TestGenerateRequestId:
    def test_returns_string(self):
        rid = protocol.generate_request_id()
        assert isinstance(rid, str)
        assert rid.startswith("req_")

    def test_unique(self):
        ids = {protocol.generate_request_id() for _ in range(100)}
        assert len(ids) == 100


class TestParseMessage:
    def test_valid_json(self):
        msg = protocol.parse_message('{"type": "hello"}')
        assert msg == {"type": "hello"}

    def test_invalid_json(self):
        assert protocol.parse_message("not json") is None

    def test_empty_string(self):
        assert protocol.parse_message("") is None


class TestValidateHello:
    def test_valid_hello(self):
        assert protocol.validate_hello({"type": "hello"}) is True

    def test_missing_type(self):
        assert protocol.validate_hello({}) is False

    def test_wrong_type(self):
        assert protocol.validate_hello({"type": "prompt"}) is False

    def test_non_dict(self):
        assert protocol.validate_hello("hello") is False


class TestValidatePrompt:
    def test_valid_prompt(self):
        msg = {"type": "prompt", "requestId": "req_123", "prompt": "Hello"}
        assert protocol.validate_prompt(msg) is True

    def test_missing_request_id(self):
        msg = {"type": "prompt", "prompt": "Hello"}
        assert protocol.validate_prompt(msg) is False

    def test_missing_prompt(self):
        msg = {"type": "prompt", "requestId": "req_123"}
        assert protocol.validate_prompt(msg) is False

    def test_prompt_too_large(self):
        msg = {"type": "prompt", "requestId": "req_123", "prompt": "x" * (MAX_PROMPT_SIZE + 1)}
        assert protocol.validate_prompt(msg) is False

    def test_extra_fields_ok(self):
        msg = {"type": "prompt", "requestId": "req_123", "prompt": "Hello", "extra": True}
        assert protocol.validate_prompt(msg) is True


class TestValidateResponse:
    def test_valid_response(self):
        msg = {"type": "response", "requestId": "req_123", "response": "Hello world"}
        assert protocol.validate_response(msg) is True

    def test_missing_request_id(self):
        msg = {"type": "response", "response": "Hello"}
        assert protocol.validate_response(msg) is False

    def test_missing_response(self):
        msg = {"type": "response", "requestId": "req_123"}
        assert protocol.validate_response(msg) is False

    def test_response_too_large(self):
        msg = {"type": "response", "requestId": "req_123", "response": "x" * (MAX_RESPONSE_SIZE + 1)}
        assert protocol.validate_response(msg) is False


class TestMessageBuilders:
    def test_make_hello_ack(self):
        msg = protocol.make_hello_ack()
        assert msg["type"] == "hello_ack"
        assert msg["protocolVersion"] == 1

    def test_make_prompt(self):
        msg = protocol.make_prompt("req_1", "Hello", 5000)
        assert msg["type"] == "prompt"
        assert msg["requestId"] == "req_1"
        assert msg["prompt"] == "Hello"
        assert msg["timeout"] == 5000

    def test_make_accepted(self):
        msg = protocol.make_accepted("req_1")
        assert msg["type"] == "accepted"
        assert msg["requestId"] == "req_1"

    def test_make_response(self):
        msg = protocol.make_response("req_1", "Hello world", "https://chatgpt.com/c/abc")
        assert msg["type"] == "response"
        assert msg["requestId"] == "req_1"
        assert msg["response"] == "Hello world"
        assert msg["url"] == "https://chatgpt.com/c/abc"

    def test_make_error(self):
        msg = protocol.make_error("req_1", ErrorCode.CHATGPT_TIMEOUT, "Timed out")
        assert msg["type"] == "error"
        assert msg["requestId"] == "req_1"
        assert msg["code"] == "CHATGPT_TIMEOUT"
        assert msg["message"] == "Timed out"

    def test_make_error_no_request_id(self):
        msg = protocol.make_error(None, ErrorCode.BRIDGE_DISABLED, "Disabled")
        assert msg["type"] == "error"
        assert "requestId" not in msg
        assert msg["code"] == "BRIDGE_DISABLED"

    def test_encode_decode_roundtrip(self):
        msg = {"type": "prompt", "requestId": "req_1", "prompt": "test"}
        encoded = protocol.encode(msg)
        decoded = protocol.parse_message(encoded)
        assert decoded == msg


class TestErrorCode:
    def test_all_codes_present(self):
        expected = [
            "CHATGPT_BRIDGE_OFFLINE",
            "BRIDGE_DISABLED",
            "NO_CHATGPT_TAB",
            "ASSIGNED_TAB_CLOSED",
            "CHATGPT_NOT_READY",
            "CHATGPT_COMPOSER_NOT_FOUND",
            "CHATGPT_SEND_BUTTON_NOT_FOUND",
            "CHATGPT_TIMEOUT",
            "CHATGPT_GENERATION_ERROR",
            "EXTENSION_DISCONNECTED",
            "INVALID_PROTOCOL_MESSAGE",
            "AUTHENTICATION_FAILED",
        ]
        for code in expected:
            assert hasattr(ErrorCode, code), f"Missing ErrorCode: {code}"
