"""``lazyagent host`` finds the engine when the TUI cannot say where it is.

The TUI announces no socket when another lazyagent already serves the repo
(embedded), or when its engine comes up after the first-connect wait
(``--remote``). The host is started with no page open, as a user does, and
the page that opens later must still get the engine and a full sidebar.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal

import pytest

from lazyagent.backend import ENGINE_ENV, ENGINE_SOCKET_ENV
from lazyagent.engine import launcher, runtime
from lazyagent.host import cli
from lazyagent.host.cli import Host, repo_root
from tests.host.conftest import _processes_of
from tests.host.test_host_integration import _ready_page
from tests.host.test_host_remote import _stop_engine

pytestmark = pytest.mark.asyncio


async def _until(condition, timeout: float = 20) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


async def _start_engine(root: str) -> str:
    """A detached engine for the repo, as ``lazyagent engine start`` runs."""
    return str(await asyncio.to_thread(launcher.spawn(root).wait))


@contextlib.asynccontextmanager
async def _host(repo, environ: dict[str, str]):
    host = Host(
        repo=None,
        cwd=str(repo),
        root=repo_root(str(repo)),
        out=io.StringIO(),
        environ=environ,
    )
    task = asyncio.get_running_loop().create_task(host.run())
    try:
        await _until(lambda: host.server.port and host.tui.pid)
        yield host
    finally:
        host.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 30)
        await _stop_engine(host.root)
        for pid in _processes_of(host.host_id):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


async def _assert_full_sidebar(host: Host, repo, tmp_path) -> None:
    page = await _ready_page(host)
    try:
        state = await page.request("get_sidebar_state")
        assert {w["path"] for w in state["worktrees"]} == {
            str(repo),
            str(tmp_path / "repo-feat"),
        }
    finally:
        await page.close()


async def test_another_lazyagent_already_serves_the_repo(repo, tmp_path):
    socket = await _start_engine(repo_root(str(repo)))
    async with _host(repo, dict(os.environ)) as host:
        # The TUI cannot take the repo's engine socket; nobody has opened
        # the page yet.
        await _until(lambda: host.server.engine_socket is not None)
        assert host.server.engine_socket == socket
        assert "another lazyagent" in host.out.getvalue()
        assert host._announced is None
        await _assert_full_sidebar(host, repo, tmp_path)


async def test_a_remote_engine_that_comes_up_late(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DISCOVERY_TIMEOUT", 1.0)
    root = repo_root(str(repo))
    socket = str(runtime.socket_path(root))
    # The TUI waits for this socket (no auto-start) and announces none
    # after its first-connect wait.
    environ = {**os.environ, ENGINE_ENV: "remote", ENGINE_SOCKET_ENV: socket}
    async with _host(repo, environ) as host:
        await _until(lambda: "no engine socket found yet" in host.out.getvalue())
        assert host.server.engine_socket is None

        # Past the host's timeout, the engine starts; the page opens after.
        assert await _start_engine(root) == socket
        await _until(lambda: host.server.engine_socket == socket, 10)
        assert "found the engine" in host.out.getvalue()
        await _assert_full_sidebar(host, repo, tmp_path)
        # The TUI announces the socket once it is connected.
        await _until(lambda: host._announced == socket, 15)
