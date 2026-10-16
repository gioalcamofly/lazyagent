"""hello: first, versioned, and answered as the schema says."""

from __future__ import annotations

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import (
    LEGACY_METHODS,
    UI_COMMAND_METHODS,
    V1_METHODS,
    validation,
)

pytestmark = pytest.mark.asyncio


async def test_hello_answers_what_the_schema_says(harness):
    client = await harness.connect(hello=False)
    params = {"protocol": 1, "client": "cli", "client_version": "test"}
    validation.validate_hello(params, strict=True)
    result = await client.request("hello", **params)
    validation.validate_hello_reply(result, strict=True)
    assert result["protocol"] == 1
    assert result["repo_root"] == harness.repo
    assert result["engine_id"]
    # No hosted UI is attached, so the ui commands are not offered.
    offered = set(LEGACY_METHODS) | set(V1_METHODS) - set(UI_COMMAND_METHODS)
    assert offered <= set(result["capabilities"])
    assert not set(UI_COMMAND_METHODS) & set(result["capabilities"])


async def test_engine_id_is_stable_across_connections(harness):
    a = await harness.connect()
    b = await harness.connect()
    assert a.hello.engine_id == b.hello.engine_id


async def test_hello_comes_first(harness):
    client = await harness.connect(hello=False)
    with pytest.raises(RequestError) as e:
        await client.request("list_worktrees")
    assert e.value.code == "PROTOCOL_ERROR"
    await client.say_hello()
    assert await client.request("list_worktrees")


async def test_input_before_hello_is_refused(harness):
    client = await harness.connect(hello=False)
    client.send_input(1, b"x")
    await client.say_hello()
    assert [e.code for e in client.errors] == ["PROTOCOL_ERROR"]


@pytest.mark.parametrize("version", [2, 0, "1"])
async def test_unsupported_version(harness, version):
    client = await harness.connect(hello=False)
    response = await client.call("hello", {"protocol": version, "client": "cli"})
    assert response.error is not None
    assert response.error.code == "UNSUPPORTED_VERSION"
    validation.validate(
        response.error.data,
        validation.load_schema("handshake.json")["$defs"]["unsupported_version.data"],
        doc="handshake.json",
        strict=True,
    )
    assert response.error.data["supported"] == [1]
    validation.validate_message(
        {"type": "res", "id": response.id, "error": response.error.to_dict()},
        strict=True,
    )
    # The connection is still usable: the client may retry with v1.
    await client.say_hello()


async def test_malformed_hello(harness):
    client = await harness.connect(hello=False)
    response = await client.call("hello", {"client": "cli"})
    assert response.error.code == "VALIDATION_ERROR"
