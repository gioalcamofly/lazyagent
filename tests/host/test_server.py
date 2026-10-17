"""HostServer with a stand-in TUI: static files, the security checks, the
page's messages, and the method whitelist (no engine needed for those)."""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from lazyagent.host import server as host_server
from lazyagent.host import websocket as ws
from lazyagent.host.cli import STATIC_ROOT
from lazyagent.host.server import ALLOWED_METHODS, HostServer
from tests.host.wsclient import Refused, WsClient, http_get, input_frame, tui_output

pytestmark = pytest.mark.asyncio

TOKEN = "t" * 43


class FakeTui:
    def __init__(self) -> None:
        self.rows, self.cols = 24, 80
        self.written: list[bytes] = []
        self.resizes: list[tuple[int, int]] = []
        self.redraws = 0

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        self.resizes.append((rows, cols))

    def redraw(self) -> None:
        self.redraws += 1

    def mode_sequence(self) -> bytes:
        return b"\x1b[?1049h"


@pytest_asyncio.fixture
async def tui():
    return FakeTui()


@pytest_asyncio.fixture
async def server(tui):
    s = HostServer(
        tui, token=TOKEN, static_root=STATIC_ROOT, repo_root="/repo", host_id="test"
    )
    await s.start()
    yield s
    await s.close()


async def _page(server, token: str = TOKEN) -> WsClient:
    """A connected, authenticated page that has read its hello."""
    client = await WsClient.connect(server.port)
    client.send_json({"type": "auth", "token": token})
    hello = json.loads(await client.recv())
    assert hello == {
        "type": "hello",
        "repo_root": "/repo",
        "host": "test",
        "version": hello["version"],
        "engine": False,
    }
    assert await client.recv() == tui_output(0, b"\x1b[?1049h")
    return client


async def _wait(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


# ---------------------------------------------------------------- HTTP


async def test_serves_the_page_with_security_headers(server):
    status, headers, body = await http_get(server.port, "/")
    assert status == 200
    assert b'<script type="module" src="app.js">' in body
    assert headers["content-type"] == "text/html; charset=utf-8"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/app.js", "text/javascript; charset=utf-8"),
        ("/app.css", "text/css; charset=utf-8"),
        ("/vendor/xterm/xterm.mjs", "text/javascript; charset=utf-8"),
        ("/vendor/xterm/addon-fit.mjs", "text/javascript; charset=utf-8"),
        ("/vendor/xterm/xterm.css", "text/css; charset=utf-8"),
    ],
)
async def test_serves_static_files(server, path, content_type):
    status, headers, body = await http_get(server.port, path)
    assert (status, headers["content-type"]) == (200, content_type)
    assert body == (STATIC_ROOT / path[1:]).read_bytes()


async def test_head_has_no_body(server):
    status, headers, body = await http_get(server.port, "/app.js", method="HEAD")
    assert status == 200 and body == b""
    assert int(headers["content-length"]) > 0


@pytest.mark.parametrize(
    "path",
    [
        "/nope",
        "/../cli.py",
        "/vendor/../../cli.py",
        "/vendor/xterm/LICENSE",
        "/vendor/xterm/VENDOR.md",
        "/%2e%2e/cli.py",
    ],
)
async def test_unknown_paths_are_404(server, path):
    status, _, _ = await http_get(server.port, path)
    assert status == 404


async def test_post_is_refused(server):
    status, _, _ = await http_get(server.port, "/", method="POST")
    assert status == 405


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "localhost:1"],
)
async def test_foreign_host_header_is_refused(server, host):
    status, _, _ = await http_get(server.port, "/", host=host.format(port=server.port))
    assert status == 403


async def test_localhost_host_header_is_accepted(server):
    status, _, _ = await http_get(server.port, "/", host=f"localhost:{server.port}")
    assert status == 200


async def test_binds_loopback_only(server):
    assert server._server.sockets[0].getsockname()[0] == "127.0.0.1"
    assert server.url == f"http://127.0.0.1:{server.port}/#token={TOKEN}"


# ---------------------------------------------------------------- upgrade checks


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://evil.example",
        "http://127.0.0.1",
        "http://127.0.0.1:1",
        "https://127.0.0.1:{port}",
        "http://evil.example:{port}",
    ],
)
async def test_foreign_origin_is_refused(server, origin):
    origin = origin.format(port=server.port) if origin else origin
    with pytest.raises(Refused) as e:
        await WsClient.connect(server.port, origin=origin)
    assert e.value.status == 403


async def test_foreign_host_on_upgrade_is_refused(server):
    with pytest.raises(Refused) as e:
        await WsClient.connect(server.port, host=f"evil.example:{server.port}")
    assert e.value.status == 403


async def test_localhost_origin_is_accepted(server):
    client = await WsClient.connect(
        server.port,
        host=f"localhost:{server.port}",
        origin=f"http://localhost:{server.port}",
    )
    await client.close()


async def test_plain_get_on_ws_path_needs_an_upgrade(server):
    status, _, _ = await http_get(server.port, "/ws")
    # No Origin on a plain GET: refused before the upgrade check.
    assert status == 403


# ---------------------------------------------------------------- auth


@pytest.mark.parametrize(
    "first",
    [
        json.dumps({"type": "auth", "token": "wrong"}),
        json.dumps({"type": "auth", "token": TOKEN[:-1]}),
        json.dumps({"type": "auth"}),
        json.dumps({"type": "req", "id": "1", "method": "get_sidebar_state"}),
        json.dumps({"type": "auth", "token": 5}),
        "not json",
        TOKEN,
    ],
)
async def test_bad_token_closes_with_policy_violation(server, tui, first):
    client = await WsClient.connect(server.port)
    client.send_text(first)
    assert await client.expect_close() == ws.CLOSE_POLICY
    assert await client.at_eof()
    assert not server.peers
    await client.close()


async def test_token_sent_as_binary_is_refused(server):
    client = await WsClient.connect(server.port)
    client.send_binary(TOKEN.encode())
    assert await client.expect_close() == ws.CLOSE_POLICY
    await client.close()


async def test_silence_is_refused(server, monkeypatch):
    monkeypatch.setattr(host_server, "AUTH_TIMEOUT", 0.2)
    client = await WsClient.connect(server.port)
    assert await client.expect_close() == ws.CLOSE_POLICY
    await client.close()


async def test_nothing_reaches_an_unauthenticated_page(server, tui):
    client = await WsClient.connect(server.port)
    client.send_binary(b"typed before auth")
    server.broadcast_output(b"secret screen")
    assert await client.expect_close() == ws.CLOSE_POLICY
    assert tui.written == []
    await client.close()


# ---------------------------------------------------------------- the page


async def test_terminal_bytes_both_ways(server, tui):
    page = await _page(server)
    server.broadcast_output(b"\x1b[1mscreen")
    assert await page.recv() == tui_output(0, b"\x1b[1mscreen")
    server.broadcast_output(b"more")
    assert await page.recv() == tui_output(10, b"more")
    page.send_binary(input_frame(b"q"))
    await _wait(lambda: [bytes(w) for w in tui.written] == [b"q"])
    await page.close()


@pytest.mark.parametrize(
    "frame",
    [b"q", b"\x02\x00\x00", tui_output(0, b"x"), input_frame(b"x", stream_id=3)],
)
async def test_bad_input_frames_are_answered(server, tui, frame):
    page = await _page(server)
    page.send_binary(frame)
    reply = json.loads(await page.recv())
    assert reply["error"]["code"] == "PROTOCOL_ERROR"
    assert tui.written == []
    await page.close()


async def test_output_goes_to_every_page(server):
    a, b = await _page(server), await _page(server)
    server.broadcast_output(b"both")
    assert await a.recv() == tui_output(0, b"both")
    assert await b.recv() == tui_output(0, b"both")
    await a.close()
    await b.close()


async def test_resize_and_redraw(server, tui):
    page = await _page(server)
    page.send_json({"type": "resize", "rows": 40, "cols": 120, "redraw": True})
    await _wait(lambda: tui.resizes == [(40, 120)])
    assert tui.redraws == 0
    page.send_json({"type": "resize", "rows": 40, "cols": 120, "redraw": True})
    await _wait(lambda: tui.redraws == 1)
    page.send_json({"type": "resize", "rows": 40, "cols": 120})
    page.send_json({"type": "resize", "rows": 0, "cols": 120})
    page.send_json({"type": "resize", "rows": 40, "cols": "120"})
    page.send_json({"type": "resize", "rows": 40, "cols": 5000})
    for _ in range(3):
        error = json.loads(await page.recv())
        assert error["error"]["code"] == "VALIDATION_ERROR"
    assert tui.resizes == [(40, 120)] and tui.redraws == 1
    await page.close()


@pytest.mark.parametrize(
    "method",
    [
        "spawn_agent",
        "send_agent_input",
        "remove_worktree",
        "create_worktree",
        "stop_agent",
        "read_agent_output",
        "set_interest",
        "refresh_worktrees",
        "hello",
        "shutdown",
    ],
)
async def test_methods_outside_the_whitelist_are_forbidden(server, method):
    page = await _page(server)
    page.send_json({"type": "req", "id": "7", "method": method, "params": {}})
    reply = json.loads(await page.recv())
    assert reply["id"] == "7"
    assert reply["error"]["code"] == "FORBIDDEN"
    await page.close()


async def test_whitelist():
    assert ALLOWED_METHODS == {
        "get_sidebar_state",
        "list_worktrees",
        "list_agents",
        "get_pr_info",
        "get_diff",
        "subscribe",
        "unsubscribe",
        "attach_stream",
        "detach_stream",
        "claim_size",
        "resize",
        "open_shell",
        "ui.get_view",
        "ui.blank",
        "ui.select_worktree",
        "ui.select_orchestrator",
        "ui.focus_agent",
        "ui.open_shell",
        "ui.show_diff",
        "ui.open_modal",
    }


async def test_allowed_method_before_the_engine_is_known(server):
    page = await _page(server)
    page.send_json({"type": "req", "id": "1", "method": "get_sidebar_state"})
    reply = json.loads(await page.recv())
    assert (reply["id"], reply["error"]["code"]) == ("1", "NOT_READY")
    await page.close()


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("[]", "PROTOCOL_ERROR"),
        ("nope", "PROTOCOL_ERROR"),
        (json.dumps({"type": "dance"}), "PROTOCOL_ERROR"),
        (json.dumps({"type": "req", "method": "list_worktrees"}), "PROTOCOL_ERROR"),
        (
            json.dumps({"type": "req", "id": "2", "method": "get_diff", "params": []}),
            "VALIDATION_ERROR",
        ),
    ],
)
async def test_malformed_messages_are_answered(server, message, code):
    page = await _page(server)
    page.send_text(message)
    reply = json.loads(await page.recv())
    assert reply["type"] == "res" and reply["error"]["code"] == code
    # The page stays connected.
    server.broadcast_output(b"still")
    assert await page.recv() == tui_output(0, b"still")
    await page.close()


async def test_oversized_message_closes_the_page(server):
    page = await _page(server)
    page.send_binary(b"x" * (host_server.MAX_MESSAGE + 1))
    assert await page.expect_close() == ws.CLOSE_TOO_BIG
    await _wait(lambda: not server.peers)
    await page.close()


async def test_slow_page_is_dropped(server, monkeypatch):
    monkeypatch.setattr(host_server, "MAX_BUFFERED", 0)
    page = await _page(server)
    (peer,) = server.peers
    monkeypatch.setattr(type(peer.socket), "buffered", property(lambda self: 1))
    server.broadcast_output(b"too much")
    assert not server.peers
    assert await page.expect_close() == ws.CLOSE_TRY_AGAIN
    await page.close()


async def test_tui_exit_is_announced_and_closes_pages(server):
    page = await _page(server)
    await server.tui_exited(0)
    assert json.loads(await page.recv()) == {"type": "exit", "code": 0}
    assert await page.expect_close() == ws.CLOSE_GOING_AWAY
    # A page that connects afterwards hears the same.
    late = await WsClient.connect(server.port)
    late.send_json({"type": "auth", "token": TOKEN})
    assert json.loads(await late.recv()) == {"type": "exit", "code": 0}
    assert await late.expect_close() == ws.CLOSE_GOING_AWAY
    await page.close()
    await late.close()
