"""Envelope round-trips and the hello handshake."""

from __future__ import annotations

import pytest

from lazyagent.protocol import PROTOCOL_VERSION, envelope, handshake, validation
from lazyagent.protocol.envelope import (
    ErrorCode,
    Event,
    ProtocolError,
    Request,
)

MESSAGES = [
    Request("r1", "list_worktrees"),
    Request("r2", "spawn_agent", {"worktree_path": "/r", "kind": "agent"}),
    envelope.ok("r1", []),
    envelope.ok("r3", None),
    envelope.error("r2", ErrorCode.NOT_SIZE_OWNER, "not the size owner", {"owner": "c2"}),
    Event(42, "engine.notice", {"severity": "warning", "message": "hi"}),
]


@pytest.mark.parametrize("message", MESSAGES, ids=lambda m: type(m).__name__)
def test_round_trip(message):
    assert envelope.loads(envelope.dumps(message)) == message
    validation.validate_message(message.to_dict(), strict=True)


def test_legacy_request_shape_plus_type():
    assert Request("u", "list_agents", {"worktree_path": "/r"}).to_dict() == {
        "type": "req",
        "id": "u",
        "method": "list_agents",
        "params": {"worktree_path": "/r"},
    }


def test_unknown_fields_are_ignored():
    parsed = envelope.parse_message(
        {"type": "evt", "seq": 1, "event": "agent.status", "data": {}, "future": 1}
    )
    assert parsed == Event(1, "agent.status", {})


def test_missing_params_default_to_empty():
    assert envelope.parse_message({"type": "req", "id": "1", "method": "m"}).params == {}


def test_error_codes_include_legacy_and_v1():
    assert {
        "UNKNOWN_METHOD", "WORKTREE_ERROR", "VALIDATION_ERROR", "TIMEOUT",
        "INTERNAL_ERROR", "UNAUTHORIZED", "UNSUPPORTED_VERSION", "NOT_SIZE_OWNER",
    } <= envelope.ERROR_CODES


# --- handshake ------------------------------------------------------------

def _hello(**overrides):
    params = handshake.Hello(PROTOCOL_VERSION, "tui", "0.9.0").to_params()
    params.update(overrides)
    return params


def test_hello_round_trip():
    params = _hello(token="t0k")
    validation.validate_hello(params, strict=True)
    assert handshake.parse_hello(params) == handshake.Hello(1, "tui", "0.9.0", "t0k")


def test_hello_reply_round_trip():
    reply = handshake.HelloReply(1, "0.9.0", "/repo", "eng-1", ("subscribe", "attach_stream"))
    result = reply.to_result()
    validation.validate_hello_reply(result, strict=True)
    assert handshake.parse_hello_reply(result) == reply


@pytest.mark.parametrize("version", [0, 2, 99])
def test_client_version_mismatch_is_a_clear_error(version):
    with pytest.raises(ProtocolError) as info:
        handshake.parse_hello(_hello(protocol=version))
    err = info.value
    assert err.code == ErrorCode.UNSUPPORTED_VERSION
    assert f"protocol {version}" in err.message
    assert "supports 1" in err.message
    assert err.data == {"supported": [1], "requested": version}
    # The engine replies with it as an ordinary error response.
    response = envelope.error("h", err.code, err.message, err.data)
    validation.validate_message(response.to_dict(), strict=True)
    validation.validate(
        response.error.data,
        validation.load_schema("handshake.json")["$defs"]["unsupported_version.data"],
        doc="handshake.json",
        strict=True,
    )


@pytest.mark.parametrize("version", ["1", 1.0, True, None])
def test_non_integer_version_is_unsupported(version):
    with pytest.raises(ProtocolError) as info:
        handshake.parse_hello(_hello(protocol=version))
    assert info.value.code == ErrorCode.UNSUPPORTED_VERSION
    assert "integer major version" in info.value.message


def test_engine_version_mismatch_is_detected_by_the_client():
    reply = handshake.HelloReply(1, "9.0.0", "/repo", "e").to_result()
    reply["protocol"] = 2
    with pytest.raises(ProtocolError) as info:
        handshake.parse_hello_reply(reply)
    assert info.value.code == ErrorCode.UNSUPPORTED_VERSION
    assert "engine speaks protocol 2" in info.value.message


@pytest.mark.parametrize(
    "params",
    [None, [], {}, {"protocol": 1}, {"protocol": 1, "client": 3}, {"protocol": 1, "client": "x", "token": 5}],
)
def test_malformed_hello_is_a_validation_error(params):
    with pytest.raises(ProtocolError) as info:
        handshake.parse_hello(params)
    assert info.value.code == ErrorCode.VALIDATION_ERROR


@pytest.mark.parametrize(
    "result",
    [None, {}, {"protocol": 1}, {"protocol": 1, "engine_version": "x", "repo_root": "/", "engine_id": "e", "capabilities": "all"}],
)
def test_malformed_hello_reply_is_a_protocol_error(result):
    with pytest.raises(ProtocolError) as info:
        handshake.parse_hello_reply(result)
    assert info.value.code == ErrorCode.PROTOCOL_ERROR
