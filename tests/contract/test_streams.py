"""attach_stream: snapshot or raw tail, then live output with no gap;
binary input; the stream ending with its session."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.protocol import validation
from tests.contract.helpers import (
    Output,
    checked,
    events_until,
    render,
    screen_lines,
    spawn,
    text_lines,
    until,
    wait_for_text,
)

pytestmark = pytest.mark.asyncio


def _attach_result(stream) -> dict:
    result = {
        "stream_id": stream.stream_id,
        "session_id": stream.session_id,
        "size": {"rows": stream.rows, "cols": stream.cols},
        "mode": stream.mode,
        "offset": stream.offset,
    }
    if stream.snapshot is not None:
        result["snapshot"] = stream.snapshot
    validation.validate_result("attach_stream", result, strict=True)
    return result


async def _idle(client, agent, timeout: float = 10.0):
    """Wait for a command to finish (it prints done and the prompt)."""
    await until(lambda: True)
    await wait_for_text(client, agent, "done", timeout=timeout)


async def test_snapshot_then_live_has_no_gap(harness, client):
    agent = await spawn(client, harness.feat)
    driver = await harness.connect()
    shell = await driver.attach(agent["session_id"])
    # Output is flowing while we attach.
    driver.send_input(shell.stream_id, b"count 400\r")
    await asyncio.sleep(0.05)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    assert _attach_result(stream)["mode"] == "snapshot"
    await wait_for_text(client, agent, "done")
    await asyncio.sleep(0.2)

    assert output.contiguous_from(stream.offset) and stream.gaps == []
    rendered = text_lines(render(stream, output))
    engine = await screen_lines(client, agent, lines=1000)
    assert rendered[-len(engine) :] == engine
    assert "line 400" in "\n".join(rendered)


async def test_offsets_increase_without_gaps(harness, client):
    agent = await spawn(client, harness.feat)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    client.send_input(stream.stream_id, b"spew 300000\r")
    await _idle(client, agent)
    await until(lambda: b"done" in output.data)
    offsets = [offset for offset, _ in output.frames]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    assert output.contiguous_from(stream.offset)
    assert len(output.data) > 300000


async def test_reattach_inside_the_ring_gets_the_raw_tail(harness, client):
    agent = await spawn(client, harness.feat)
    first = Output()
    stream = await client.attach(agent["session_id"], first)
    client.send_input(stream.stream_id, b"count 50\r")
    await until(lambda: b"done" in first.data)
    resume = stream.offset + len(first.data) // 2
    await client.detach(stream.stream_id)

    other = await harness.connect()
    tail = Output()
    again = await other.attach(agent["session_id"], tail, from_offset=resume)
    assert _attach_result(again)["mode"] == "tail"
    assert again.offset == resume and again.snapshot is None
    await until(lambda: len(tail.data) >= len(first.data) - (resume - stream.offset))
    assert tail.data.startswith(first.data[resume - stream.offset :])


@pytest.mark.parametrize("where", ["too old", "in the future"])
async def test_reattach_outside_the_ring_gets_a_snapshot(harness, client, where):
    agent = await spawn(client, harness.feat)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    if where == "too old":
        # Further back than a raw tail is allowed to reach (512 KiB), with
        # room to spare. Only the outcome matters here, not how fast the
        # megabyte goes through, so a loaded machine gets a long timeout.
        client.send_input(stream.stream_id, b"spew 1000000\r")
        await _idle(client, agent, timeout=60)
        from_offset = 0
    else:
        from_offset = 10**12
    other = await harness.connect()
    again = await other.attach(agent["session_id"], Output(), from_offset=from_offset)
    assert _attach_result(again)["mode"] == "snapshot"
    assert again.snapshot


async def test_input_frames_reach_the_process(harness, client):
    agent = await spawn(client, harness.feat)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    client.send_input(stream.stream_id, b"typed by v1\r")
    await wait_for_text(client, agent, "got: 'typed by v1'")
    await until(lambda: b"typed by v1" in output.data)


async def test_shell_stream(harness, client):
    shell = await checked(client, "open_shell", worktree_path=harness.feat)
    output = Output()
    stream = await client.attach(shell["session_id"], output)
    client.send_input(stream.stream_id, b"echo V1_$((40+2))\r")
    await until(lambda: b"V1_42" in output.data, timeout=15)


async def test_two_clients_get_the_same_bytes(harness, client):
    agent = await spawn(client, harness.feat)
    other = await harness.connect()
    a, b = Output(), Output()
    sa = await client.attach(agent["session_id"], a)
    sb = await other.attach(agent["session_id"], b)
    client.send_input(sa.stream_id, b"count 30\r")
    await until(lambda: b"done" in a.data and b"done" in b.data)
    start = max(sa.offset, sb.offset)
    assert a.data[start - sa.offset :] == b.data[start - sb.offset :]
    listed = await checked(client, "list_agents", worktree_path=harness.feat)
    assert listed["agents"][0]["attached_clients"] == 2


async def test_detach_stops_the_output(harness, client):
    agent = await spawn(client, harness.feat)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    result = await checked(client, "detach_stream", stream_id=stream.stream_id)
    assert result == {"stream_id": stream.stream_id, "detached": True}
    other = await harness.connect()
    driver = await other.attach(agent["session_id"])
    other.send_input(driver.stream_id, b"count 5\r")
    await wait_for_text(client, agent, "done")
    await asyncio.sleep(0.1)
    assert output.frames == []
    client.send_input(stream.stream_id, b"x")
    await checked(client, "list_worktrees")
    assert [e.code for e in client.errors] == ["PROTOCOL_ERROR"]


async def test_the_stream_closes_with_its_session(harness, client):
    agent = await spawn(client, harness.feat)
    stream = await client.attach(agent["session_id"], Output())
    await checked(client, "stop_agent", worktree_path=harness.feat)
    [closed] = await events_until(client, lambda e: e.event == "stream.closed")
    validation.validate_event("stream.closed", closed.data, strict=True)
    assert closed.data["stream_id"] == stream.stream_id
    assert stream.ended == "closed"


async def test_attach_unknown_session(client):
    response = await client.call("attach_stream", {"session_id": "nope"})
    assert response.error.code == "VALIDATION_ERROR"


async def test_a_snapshot_right_after_a_hidden_burst_is_exact(harness, client):
    """Nobody watches while the agent prints; the attach that follows may
    get a screen the engine has not caught up on, at the offset it is exact
    at, and the rest as frames: rendered together they are the engine's
    screen, with no gap."""
    agent = await spawn(client, harness.feat)
    driver = await harness.connect()
    raw = Output()
    shell = await driver.attach(agent["session_id"], raw)
    driver.send_input(shell.stream_id, b"spew 400000\r")
    await until(lambda: b"done" in raw.data[-4096:], timeout=60)
    output = Output()
    stream = await client.attach(agent["session_id"], output)
    assert _attach_result(stream)["mode"] == "snapshot"
    await until(lambda: b"done" in output.data[-4096:] or b"done" in (
        stream.snapshot or ""
    ).encode(), timeout=60)
    await wait_for_text(client, agent, "your turn>", timeout=60)
    await asyncio.sleep(0.2)
    assert output.contiguous_from(stream.offset) and stream.gaps == []
    rendered = text_lines(render(stream, output))
    engine = await screen_lines(client, agent, lines=24)
    assert rendered[-len(engine) :] == engine
