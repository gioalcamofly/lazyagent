"""Message envelope for protocol v1: requests, responses, errors and events.

Every JSON control message is an object with a ``type`` discriminator:

    Request:  {"type": "req", "id": "…", "method": "…", "params": {…}}
    Success:  {"type": "res", "id": "…", "result": …}
    Error:    {"type": "res", "id": "…", "error": {"code", "message", "data?"}}
    Event:    {"type": "evt", "seq": 123, "event": "agent.status", "data": {…}}

This is the legacy NDJSON IPC shape (``{id, method, params}`` /
``{id, result|error}``) plus ``type``. Unknown fields are ignored on parse,
so additive changes need no version bump.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    """Error codes carried in ``error.code``.

    The first five are the legacy IPC codes; the rest are new in v1. Codes are
    open strings: a peer must handle a code it does not know.
    """

    UNKNOWN_METHOD = "UNKNOWN_METHOD"
    WORKTREE_ERROR = "WORKTREE_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    TIMEOUT = "TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNAUTHORIZED = "UNAUTHORIZED"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    NOT_SIZE_OWNER = "NOT_SIZE_OWNER"
    # A ui.* command reached an engine with no hosted UI to carry it out.
    NO_HOSTED_UI = "NO_HOSTED_UI"
    # A write whose if_rev is stale (session notes); data has {rev, text}.
    CONFLICT = "CONFLICT"
    # Raised locally when a message or frame cannot be parsed.
    PROTOCOL_ERROR = "PROTOCOL_ERROR"


ERROR_CODES: frozenset[str] = frozenset(
    v for k, v in vars(ErrorCode).items() if k.isupper()
)


class ProtocolError(Exception):
    """A message that violates the protocol; ``code`` is an :class:`ErrorCode`."""

    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_error(self) -> ErrorInfo:
        return ErrorInfo(self.code, self.message, self.data)


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "req", "id": self.id, "method": self.method, "params": self.params}


@dataclass(frozen=True)
class Response:
    """A reply to the request with the same ``id``: ``result`` xor ``error``."""

    id: str
    result: Any = None
    error: ErrorInfo | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        if self.error is not None:
            return {"type": "res", "id": self.id, "error": self.error.to_dict()}
        return {"type": "res", "id": self.id, "result": self.result}


@dataclass(frozen=True)
class Event:
    """A pushed event. ``seq`` is one engine-wide monotonic counter."""

    seq: int
    event: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": "evt", "seq": self.seq, "event": self.event, "data": self.data}


Message = Request | Response | Event


def ok(request_id: str, result: Any) -> Response:
    return Response(request_id, result=result)


def error(request_id: str, code: str, message: str, data: Any = None) -> Response:
    return Response(request_id, error=ErrorInfo(code, message, data))


def _fail(message: str) -> ProtocolError:
    return ProtocolError(ErrorCode.PROTOCOL_ERROR, message)


def _require_str(obj: dict[str, Any], key: str, kind: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise _fail(f"{kind} {key!r} must be a string, got {type(value).__name__}")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_message(obj: Any) -> Message:
    """Build a :class:`Message` from a decoded JSON object.

    Raises :class:`ProtocolError` (``PROTOCOL_ERROR``) on a malformed
    envelope. Payload contents (params/result/data) are not checked here;
    see :mod:`lazyagent.protocol.validation`.
    """
    if not isinstance(obj, dict):
        raise _fail(f"message must be a JSON object, got {type(obj).__name__}")
    kind = obj.get("type")
    if kind == "req":
        request_id = _require_str(obj, "id", "request")
        method = _require_str(obj, "method", "request")
        params = obj.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise _fail("request 'params' must be an object")
        return Request(request_id, method, params)
    if kind == "res":
        request_id = _require_str(obj, "id", "response")
        has_result = "result" in obj
        has_error = "error" in obj
        if has_result == has_error:
            raise _fail("response must have exactly one of 'result' or 'error'")
        if has_result:
            return Response(request_id, result=obj["result"])
        err = obj["error"]
        if not isinstance(err, dict):
            raise _fail("response 'error' must be an object")
        code = _require_str(err, "code", "error")
        message = _require_str(err, "message", "error")
        return Response(request_id, error=ErrorInfo(code, message, err.get("data")))
    if kind == "evt":
        seq = obj.get("seq")
        if not _is_int(seq) or seq < 0:
            raise _fail("event 'seq' must be a non-negative integer")
        name = _require_str(obj, "event", "event")
        return Event(seq, name, obj.get("data"))
    raise _fail(f"unknown message type {kind!r}; expected 'req', 'res' or 'evt'")


def dumps(message: Message) -> bytes:
    """Serialise a message to compact UTF-8 JSON."""
    return json.dumps(message.to_dict(), separators=(",", ":")).encode()


def loads(payload: bytes | str) -> Message:
    """Parse UTF-8 JSON into a :class:`Message`; raises :class:`ProtocolError`."""
    try:
        obj = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as e:
        raise _fail(f"invalid JSON: {e}") from None
    return parse_message(obj)
