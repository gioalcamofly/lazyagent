"""``lazyagent host`` end to end: the real TUI in hosted mode on the host's
PTY, in a temporary git repo, driven through the page's WebSocket the way
app.js drives it."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import re
import signal
import struct
import termios

import pytest

from lazyagent.host.cli import Host
from tests.contract.helpers import wait_for_text
from tests.host.wsclient import WsClient, input_frame, output_frame

pytestmark = pytest.mark.asyncio

_ANSI = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\x1b[=>]"
)


class Page:
    """What app.js does, over a raw WebSocket: binary frames pile up as the
    terminal, JSON messages are queued, requests are matched by id."""

    def __init__(self, client: WsClient) -> None:
        self.client = client
        self.screen = bytearray()
        # Output of the page's own streams, by stream id.
        self.streams: dict[int, bytearray] = {}
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        self.replies: dict[str, asyncio.Future] = {}
        self.ids = 0
        self.closed: int | None = None
        self.changed = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._read())

    @classmethod
    async def open(cls, host: Host) -> Page:
        client = await WsClient.connect(host.server.port)
        client.send_json({"type": "auth", "token": host.server.token})
        page = cls(client)
        hello = await page.message("hello")
        assert hello["repo_root"] == host.root
        return page

    async def _read(self) -> None:
        while True:
            try:
                message = await self.client.recv(timeout=60)
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                return
            if isinstance(message, bytes):
                stream_id, _, data = output_frame(message)
                if stream_id == 0:
                    self.screen += data
                else:
                    self.streams.setdefault(stream_id, bytearray()).extend(data)
            elif isinstance(message, tuple):
                self.closed = message[0]
                self.changed.set()
                return
            else:
                data = json.loads(message)
                if data["type"] == "res" and data["id"] in self.replies:
                    self.replies.pop(data["id"]).set_result(data)
                else:
                    self.messages.put_nowait(data)
            self.changed.set()

    async def message(self, kind: str, timeout: float = 30, **fields) -> dict:
        async def find() -> dict:
            while True:
                data = await self.messages.get()
                if data["type"] == kind and all(
                    data.get(k) == v for k, v in fields.items()
                ):
                    return data

        return await asyncio.wait_for(find(), timeout)

    async def event(self, name: str, timeout: float = 30, **fields) -> dict:
        async def find() -> dict:
            while True:
                data = await self.messages.get()
                if data.get("event") == name and all(
                    data["data"].get(k) == v for k, v in fields.items()
                ):
                    return data["data"]

        return await asyncio.wait_for(find(), timeout)

    async def call(self, method: str, **params) -> dict:
        self.ids += 1
        request_id = str(self.ids)
        future = asyncio.get_running_loop().create_future()
        self.replies[request_id] = future
        self.client.send_json(
            {"type": "req", "id": request_id, "method": method, "params": params}
        )
        return await asyncio.wait_for(future, 30)

    async def request(self, method: str, **params):
        reply = await self.call(method, **params)
        assert "error" not in reply, reply
        return reply["result"]

    def text(self) -> str:
        return _ANSI.sub(b"", bytes(self.screen)).decode(errors="replace")

    async def wait_text(self, needle: str, count: int = 1, timeout: float = 20) -> None:
        async def find() -> None:
            while self.text().count(needle) < count:
                self.changed.clear()
                await self.changed.wait()

        try:
            await asyncio.wait_for(find(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"{needle!r} x{count} not on screen") from None

    def stream_text(self, stream_id: int) -> str:
        return _ANSI.sub(b"", bytes(self.streams.get(stream_id, b""))).decode(
            errors="replace"
        )

    async def wait_stream(self, stream_id: int, needle: str, timeout: float = 20) -> None:
        async def find() -> None:
            while needle not in self.stream_text(stream_id):
                self.changed.clear()
                await self.changed.wait()

        try:
            await asyncio.wait_for(find(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(
            f"{needle!r} not in stream {stream_id}: {self.stream_text(stream_id)!r}"
        ) from None

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        await self.client.close()


async def _ready_page(host: Host) -> Page:
    page = await Page.open(host)
    page.client.send_json({"type": "resize", "rows": 30, "cols": 110, "redraw": True})
    await page.message("engine", ready=True)
    return page


async def test_page_drives_the_hosted_tui(host, repo, tmp_path):
    page = await _ready_page(host)
    try:
        feat = str(tmp_path / "repo-feat")
        assert host.server.engine_socket is not None
        assert host.announcements[0]["socket"] == host.server.engine_socket

        sidebar = await page.request("get_sidebar_state")
        assert {w["path"] for w in sidebar["worktrees"]} == {str(repo), feat}
        assert sidebar["orchestrator"] == {"agents": []}
        await page.request(
            "subscribe", since=sidebar["cursor"], events=["ui.view_changed"]
        )

        # The terminal shows the TUI.
        await page.wait_text("Select a worktree")

        result = await page.request("ui.select_worktree", worktree_path=feat)
        assert result["command"] == "select_worktree"
        await page.event("ui.view_changed", worktree_path=feat, focus="shell")

        # Keystrokes from the page reach the worktree shell.
        page.client.send_binary(input_frame(b"echo H2_IT_OK\r"))
        await page.wait_text("H2_IT_OK", count=2)

        # A resize from the page resizes the PTY; the TUI announces again.
        seen = len(host.announcements)
        page.client.send_json({"type": "resize", "rows": 32, "cols": 120})
        for _ in range(500):
            if len(host.announcements) > seen:
                break
            await asyncio.sleep(0.02)
        assert len(host.announcements) > seen
        size = fcntl.ioctl(host.tui._fd, termios.TIOCGWINSZ, b"\0" * 8)
        assert struct.unpack("HHHH", size)[:2] == (32, 120)

        # Read-only data and view commands pass; anything else is refused.
        worktrees = await page.request("list_worktrees")
        assert len(worktrees) == 2
        diff = await page.request("get_diff", worktree_path=feat)
        assert "diff" in diff
        await page.request("ui.show_diff")
        await page.event("ui.view_changed", worktree_path=feat, focus="diff")
        for method, params in [
            ("spawn_agent", {"worktree_path": feat}),
            ("remove_worktree", {"worktree_path": feat}),
            ("send_agent_input", {"worktree_path": feat, "agent_id": "1", "text": "x"}),
        ]:
            reply = await page.call(method, **params)
            assert reply["error"]["code"] == "FORBIDDEN"
        bad = await page.call("ui.select_worktree", worktree_path="/nowhere")
        assert bad["error"]["code"] == "VALIDATION_ERROR"

        # The spawn button's command opens the TUI's modal.
        await page.request("ui.open_modal", modal="spawn")
        await page.event("ui.view_changed", focus="modal", modal="spawn")
        await page.wait_text("Spawn agent in feat")
        page.client.send_binary(input_frame(b"\x1b"))
        await page.event("ui.view_changed", modal=None)

        # q in the TUI (Ctrl+K first, out of the shell) ends the host.
        page.client.send_binary(input_frame(b"\x0b"))
        await asyncio.sleep(0.3)
        page.client.send_binary(input_frame(b"q"))
        assert await page.message("exit") == {"type": "exit", "code": 0}
        assert await asyncio.wait_for(host.task, 30) == 0
        for _ in range(100):
            if page.closed is not None:
                break
            await asyncio.sleep(0.05)
        assert page.closed == 1001
    finally:
        await page.close()


async def test_stop_quits_the_tui_cleanly(host, tmp_path):
    page = await _ready_page(host)
    try:
        # A worktree shell is running when the host is told to stop.
        await page.request(
            "ui.select_worktree", worktree_path=str(tmp_path / "repo-feat")
        )
        await page.wait_text("$", timeout=20)
        host.stop()
        assert await asyncio.wait_for(host.task, 30) == 0
        assert (await page.message("exit"))["code"] == 0
        assert "lazyagent exited (0)" in host.out.getvalue()
    finally:
        await page.close()


async def test_a_killed_tui_ends_the_host(host, tmp_path):
    page = await _ready_page(host)
    try:
        await page.request(
            "ui.select_worktree", worktree_path=str(tmp_path / "repo-feat")
        )
        await page.wait_text("$", timeout=20)
        os.kill(host.tui.pid, signal.SIGKILL)
        assert await asyncio.wait_for(host.task, 30) == -signal.SIGKILL
        assert (await page.message("exit"))["code"] == -signal.SIGKILL
    finally:
        await page.close()


async def test_native_terminals_through_the_host(fake_claude, host, tmp_path):
    """U2: the page attaches the streams of an agent and a shell on its own
    engine connection; frames carry their stream id; input frames for a
    stream reach its process; the hosted TUI can be blanked."""
    from lazyagent.engine.client import Client

    page = await _ready_page(host)
    feat = str(tmp_path / "repo-feat")
    assert host.server.engine_socket is not None
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        agent = await driver.request("spawn_agent", worktree_path=feat)
        await wait_for_text(driver, agent, "your turn>")
        sidebar = await page.request("get_sidebar_state")
        await page.request("subscribe", since=sidebar["cursor"])
        # (The TUI shows nothing yet, so its view does not change: only the
        # placeholder's text does.)
        await page.request("ui.blank")
        await page.wait_text("Shown in the browser")

        attached = await page.request("attach_stream", session_id=agent["session_id"])
        stream_id = attached["stream_id"]
        assert stream_id > 0 and attached["mode"] == "snapshot"
        assert "fake agent ready" in attached["snapshot"]
        claim = await page.request(
            "claim_size", session_id=agent["session_id"], rows=30, cols=100
        )
        assert (claim["rows"], claim["cols"]) == (30, 100)
        await page.event("stream.resized", stream_id=stream_id, rows=30, cols=100)
        page.client.send_binary(input_frame(b"size\r", stream_id))
        await page.wait_stream(stream_id, "size 30x100")

        # Input for a stream the page does not own is refused.
        page.client.send_binary(input_frame(b"x", stream_id + 100))
        error = await page.message("res", id="")
        assert error["error"]["code"] == "PROTOCOL_ERROR"

        shell = await page.request("open_shell", worktree_path=feat)
        attached_shell = await page.request("attach_stream", session_id=shell["session_id"])
        shell_stream = attached_shell["stream_id"]
        assert shell_stream != stream_id
        page.client.send_binary(input_frame(b"echo U2_SHELL_$((6*7))\r", shell_stream))
        await page.wait_stream(shell_stream, "U2_SHELL_42")

        # Re-attach from where the page is: a tail, not a snapshot.
        offset = attached["offset"] + len(page.streams[stream_id])
        detached = await page.request("detach_stream", stream_id=stream_id)
        assert detached == {"stream_id": stream_id, "detached": True}
        again = await page.request(
            "attach_stream", session_id=agent["session_id"], from_offset=offset
        )
        assert again["mode"] == "tail" and again["offset"] == offset

        # The agent exits: its stream closes.
        page.client.send_binary(input_frame(b"exit 0\r", again["stream_id"]))
        closed = await page.event("stream.closed", stream_id=again["stream_id"])
        assert closed["session_id"] == agent["session_id"]
        gone = await page.request("detach_stream", stream_id=again["stream_id"])
        assert gone["detached"] is False
    finally:
        await driver.close()
        await page.close()


async def test_a_slow_page_loses_the_stream_not_the_page(
    fake_claude, host, tmp_path, monkeypatch
):
    from lazyagent.engine.client import Client
    from lazyagent.host import server as host_server

    page = await _ready_page(host)
    feat = str(tmp_path / "repo-feat")
    driver = await Client.connect(host.server.engine_socket, client="cli")
    try:
        agent = await driver.request("spawn_agent", worktree_path=feat)
        await wait_for_text(driver, agent, "your turn>")
        await page.request("subscribe")
        attached = await page.request("attach_stream", session_id=agent["session_id"])
        (peer,) = host.server.peers
        monkeypatch.setattr(host_server, "MAX_STREAM_BUFFERED", -1)
        page.client.send_binary(input_frame(b"count 50\r", attached["stream_id"]))
        reset = await page.event("stream.reset", stream_id=attached["stream_id"])
        assert reset["session_id"] == agent["session_id"]
        assert attached["stream_id"] in peer.dropped
        monkeypatch.setattr(host_server, "MAX_STREAM_BUFFERED", 2 * 1024 * 1024)
        # Still connected: it re-attaches from the reset's offset.
        again = await page.request(
            "attach_stream", session_id=agent["session_id"], from_offset=reset["offset"]
        )
        assert again["offset"] == reset["offset"] or again["mode"] == "snapshot"
        page.client.send_binary(input_frame(b"size\r", again["stream_id"]))
        await page.wait_stream(again["stream_id"], "size ")
    finally:
        await driver.close()
        await page.close()
