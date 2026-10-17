"""``lazyagent host`` on a detached engine (``--remote``): the hosted TUI
starts the engine, the page spawns and types into an agent, the host quits
and the agent keeps running, a new host comes back to it (engine-split §6f,
checklist item 5)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal

import pytest

from lazyagent.backend import ENGINE_ENV
from lazyagent.engine import launcher
from lazyagent.engine.client import Client
from lazyagent.host.cli import Host, repo_root
from tests.host.conftest import _processes_of
from tests.host.test_host_integration import _ready_page
from tests.host.wsclient import input_frame

async def _start(repo) -> tuple[Host, asyncio.Task]:
    # What `lazyagent host --remote` passes the TUI.
    host = Host(
        repo=None,
        cwd=str(repo),
        root=repo_root(str(repo)),
        out=io.StringIO(),
        environ={**os.environ, ENGINE_ENV: "remote"},
    )
    task = asyncio.get_running_loop().create_task(host.run())
    for _ in range(500):
        if host.server.port and host.tui.pid:
            break
        await asyncio.sleep(0.01)
    return host, task


async def _quit(host: Host, task: asyncio.Task) -> None:
    host.stop()
    await asyncio.wait_for(task, 30)


@pytest.mark.asyncio
async def test_a_host_on_a_detached_engine(fake_claude, repo, tmp_path):
    root = repo_root(str(repo))
    feat = str(tmp_path / "repo-feat")
    hosts: list[Host] = []
    try:
        first, task = await _start(repo)
        hosts.append(first)
        page = await _ready_page(first)
        info = launcher.info(root)
        assert info is not None and info["detached"]
        engine_pid = info["pid"]
        assert engine_pid != first.tui.pid and os.getsid(engine_pid) == engine_pid

        agent = await page.request("spawn_agent", worktree_path=feat)
        attached = await page.request("attach_stream", session_id=agent["session_id"])
        sid = attached["stream_id"]
        await page.wait_stream(sid, "your turn>")
        page.client.send_binary(input_frame(b"hello\r", sid))
        await page.wait_stream(sid, "got: 'hello'")
        await page.close()
        await _quit(first, task)

        # The host and its TUI are gone; the engine and the agent are not,
        # and nothing that outlives the host thinks it is hosted.
        assert _processes_of(first.host_id) == []
        assert "agents keep running in the engine" in first.out.getvalue()
        assert launcher.info(root)["pid"] == engine_pid
        async with _client(root) as client:
            listed = await client.request("list_agents", worktree_path=feat)
            assert [a["agent_id"] for a in listed["agents"]] == [agent["agent_id"]]

        second, task = await _start(repo)
        hosts.append(second)
        page = await _ready_page(second)
        try:
            state = await page.request("get_sidebar_state")
            [wt] = [w for w in state["worktrees"] if w["path"] == feat]
            assert [a["agent_id"] for a in wt["agents"]] == [agent["agent_id"]]
            again = await page.request("attach_stream", session_id=agent["session_id"])
            assert again["mode"] == "snapshot"
            assert "got: 'hello'" in again["snapshot"]
        finally:
            await page.close()
        await _quit(second, task)
    finally:
        await _stop_engine(root)
        for host in hosts:
            for pid in _processes_of(host.host_id):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


@contextlib.asynccontextmanager
async def _client(root: str):
    path = launcher.find(root)
    assert path is not None
    client = await Client.connect(path, client="cli")
    try:
        yield client
    finally:
        await client.close()


async def _stop_engine(root: str) -> None:
    info = launcher.info(root)
    if info is None:
        return
    async with _client(root) as client:
        await client.request("shutdown")
    for _ in range(300):
        try:
            os.kill(info["pid"], 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.02)
    os.kill(info["pid"], signal.SIGKILL)


def test_the_remote_flag_runs_the_hosted_tui_on_the_engine(repo, monkeypatch):
    from lazyagent.host import cli

    made = {}

    class FakeHost:
        def __init__(self, **options):
            made.update(options)

        async def run(self):
            return 0

    monkeypatch.setattr(cli, "Host", FakeHost)
    monkeypatch.chdir(repo)
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--remote"])
    assert exit_.value.code == 0
    assert made["environ"][ENGINE_ENV] == "remote"
    made.clear()
    monkeypatch.delenv(ENGINE_ENV, raising=False)
    with pytest.raises(SystemExit):
        cli.main([])
    assert ENGINE_ENV not in made["environ"]
