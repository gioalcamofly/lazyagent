"""Handshake and versioning.

The first message on a connection is a ``hello`` request::

    {"type":"req","id":"1","method":"hello",
     "params":{"protocol":1,"client":"tui","client_version":"0.9.0","token":"…"}}

and the engine answers with a ``hello`` result::

    {"protocol":1,"engine_version":"0.9.0","capabilities":[…],
     "repo_root":"/path","engine_id":"…"}

``protocol`` is the major version. A mismatch on either side is an
``UNSUPPORTED_VERSION`` error whose ``data`` lists what is supported.
Additive changes need no bump; they are advertised in ``capabilities``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .envelope import ErrorCode, ProtocolError

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS: tuple[int, ...] = (PROTOCOL_VERSION,)

HELLO_METHOD = "hello"

# Known client kinds; the field is an open string so new clients need no bump.
CLIENT_KINDS: tuple[str, ...] = ("tui", "web", "mcp", "cli")


@dataclass(frozen=True)
class Hello:
    protocol: int
    client: str
    client_version: str = ""
    token: str | None = None

    def to_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocol": self.protocol,
            "client": self.client,
            "client_version": self.client_version,
        }
        if self.token is not None:
            out["token"] = self.token
        return out


@dataclass(frozen=True)
class HelloReply:
    protocol: int
    engine_version: str
    repo_root: str
    engine_id: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    # Additive: whether ``shutdown`` can stop it (not inside a TUI), and the
    # commit of the lazyagent source it runs (None for a wheel install).
    detached: bool | None = None
    source_commit: str | None = None

    def to_result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "protocol": self.protocol,
            "engine_version": self.engine_version,
            "capabilities": list(self.capabilities),
            "repo_root": self.repo_root,
            "engine_id": self.engine_id,
        }
        if self.detached is not None:
            out["detached"] = self.detached
            out["source_commit"] = self.source_commit
        return out


def _check_version(value: Any, who: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"{who} sent protocol={value!r}; expected an integer major version "
            f"(supported: {list(SUPPORTED_VERSIONS)})",
            {"supported": list(SUPPORTED_VERSIONS), "requested": value},
        )
    if value not in SUPPORTED_VERSIONS:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION,
            f"{who} speaks protocol {value}, this side supports "
            f"{', '.join(map(str, SUPPORTED_VERSIONS))}. Upgrade the older side.",
            {"supported": list(SUPPORTED_VERSIONS), "requested": value},
        )
    return value


def _check_str(obj: dict[str, Any], key: str, who: str, default: str | None = None) -> str:
    value = obj.get(key, default)
    if not isinstance(value, str):
        raise ProtocolError(
            ErrorCode.VALIDATION_ERROR, f"{who} {key!r} must be a string"
        )
    return value


def parse_hello(params: Any) -> Hello:
    """Engine side: validate a client's hello params.

    Raises :class:`ProtocolError` with ``UNSUPPORTED_VERSION`` on a version
    mismatch, ``VALIDATION_ERROR`` on a malformed hello.
    """
    if not isinstance(params, dict):
        raise ProtocolError(ErrorCode.VALIDATION_ERROR, "hello params must be an object")
    if "protocol" not in params:
        raise ProtocolError(ErrorCode.VALIDATION_ERROR, "hello is missing 'protocol'")
    protocol = _check_version(params["protocol"], "client")
    client = _check_str(params, "client", "hello")
    client_version = _check_str(params, "client_version", "hello", "")
    token = params.get("token")
    if token is not None and not isinstance(token, str):
        raise ProtocolError(ErrorCode.VALIDATION_ERROR, "hello 'token' must be a string")
    return Hello(protocol, client, client_version, token)


def parse_hello_reply(result: Any) -> HelloReply:
    """Client side: validate the engine's hello result."""
    if not isinstance(result, dict):
        raise ProtocolError(ErrorCode.PROTOCOL_ERROR, "hello result must be an object")
    if "protocol" not in result:
        raise ProtocolError(ErrorCode.PROTOCOL_ERROR, "hello result is missing 'protocol'")
    protocol = _check_version(result["protocol"], "engine")
    capabilities = result.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise ProtocolError(
            ErrorCode.PROTOCOL_ERROR, "hello 'capabilities' must be a list of strings"
        )
    try:
        return HelloReply(
            protocol=protocol,
            engine_version=_check_str(result, "engine_version", "hello result"),
            repo_root=_check_str(result, "repo_root", "hello result"),
            engine_id=_check_str(result, "engine_id", "hello result"),
            capabilities=tuple(capabilities),
        )
    except ProtocolError as e:
        raise ProtocolError(ErrorCode.PROTOCOL_ERROR, e.message) from None
