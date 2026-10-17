"""Where the socket lives and who may use it: runtime dir, permissions,
engine.json, peer uid, and not stealing another live engine's socket.

These look at the files the engine leaves around, so they run against the
Python engine only.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import socket
import stat
from pathlib import Path

import pytest

from lazyagent.engine import runtime
from lazyagent.engine import server as v1
from lazyagent.engine.client import Client, ConnectionClosed, discover
from lazyagent.engine.events import EngineEvent
from tests.contract.helpers import checked
from tests.engine.fakes import make_engine, worktree

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _python_only(harness):
    if harness.name != "python":
        pytest.skip("inspects the Python engine's files")


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


async def test_socket_and_info_file(harness, runtime_dir):
    path = Path(harness.socket_path)
    assert path == v1.runtime_dir(harness.repo) / "engine.sock"
    assert path.parent.parent == Path(runtime_dir) / "lazyagent"
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
    info_path = path.with_name("engine.json")
    assert _mode(info_path) == 0o600
    info = json.loads(info_path.read_text())
    assert info["pid"] == os.getpid()
    assert info["protocol"] == 1
    assert info["socket"] == str(path)
    assert info["engine_id"] == harness.server.engine_id
    assert info["version"]


async def test_stop_removes_the_socket_and_the_dir(harness):
    path = Path(harness.socket_path)
    await harness.server.stop()
    assert not path.exists()
    assert not path.with_name("engine.json").exists()
    assert not path.parent.exists()
    assert not path.parent.parent.exists()  # no other repo's dir was in it


async def test_runtime_dir_falls_back_to_tmp():
    path = v1.runtime_dir("/some/repo", environ={})
    assert path.parent.name == f"lazyagent-{os.getuid()}"
    assert len(path.name) == 12


async def test_a_foreign_uid_is_refused(harness, monkeypatch):
    monkeypatch.setattr(v1, "peer_uid", lambda sock: os.getuid() + 1)
    with pytest.raises((ConnectionClosed, ConnectionError)):
        await Client.connect(harness.socket_path)


async def test_a_live_socket_is_left_alone(harness, tmp_path):
    info_before = Path(harness.socket_path).with_name("engine.json").read_text()
    second = v1.V1Server(harness.engine, harness.socket_path)
    assert await second.start() is False
    await second.stop()
    # The first engine still answers and still owns the files.
    client = await harness.connect()
    assert client.hello.engine_id == harness.server.engine_id
    assert Path(harness.socket_path).with_name("engine.json").read_text() == info_before


async def test_a_dead_socket_is_replaced(harness):
    path = harness.socket_path
    await harness.server.stop()
    stale = socket.socket(socket.AF_UNIX)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stale.bind(path)
    stale.close()  # the file stays, nobody listens
    server = v1.V1Server(harness.engine, path)
    assert await server.start() is True
    client = await Client.connect(path)
    assert client.hello.engine_id == server.engine_id
    await client.close()
    await server.stop()
    harness.server = server


async def test_a_second_engine_on_the_same_repo_warns_and_runs_without_v1(harness):
    other = make_engine([worktree(harness.repo)], repo_root=harness.repo)
    events: list[EngineEvent] = []
    other.subscribe(events.append)
    # A real start, with the legacy IPC server too, as the TUI does it.
    await other.start()
    try:
        assert other.v1_socket_path is None
        notices = [e.data for e in events if e.event == "engine.notice"]
        assert notices and notices[-1]["severity"] == "warning"
        assert harness.socket_path in notices[-1]["message"]
        assert other.socket_path is not None  # the legacy server still runs
    finally:
        await other.stop()
    assert os.path.exists(harness.socket_path)


async def test_the_engine_starts_and_stops_the_server(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    engine = make_engine([worktree(str(repo))], repo_root=str(repo))
    await engine.start()
    try:
        path = engine.v1_socket_path
        assert path == str(v1.socket_path(str(repo)))
        client = await Client.connect(path)
        await checked(client, "subscribe")
    finally:
        await engine.stop()
    shutdown = await client.next_event(lambda e: e.event == "engine.shutdown")
    assert shutdown.data == {"reason": "stopped"}
    await asyncio.wait_for(client.wait_closed(), 5)
    await client.close()
    assert not os.path.exists(path)
    assert engine.v1_socket_path is None


async def test_the_server_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LAZYAGENT_V1_SERVER", "0")
    repo = tmp_path / "off"
    repo.mkdir()
    engine = make_engine([worktree(str(repo))], repo_root=str(repo))
    await engine.start()
    try:
        assert engine.v1_socket_path is None
        assert not v1.socket_path(str(repo)).exists()
        assert engine.socket_path is not None
    finally:
        await engine.stop()


async def test_discover_finds_the_socket_from_the_repo_root(harness):
    assert discover(harness.repo) == Path(harness.socket_path)


async def test_discover_without_an_engine(harness, tmp_path):
    assert discover(str(tmp_path / "elsewhere")) is None
    await harness.server.stop()
    assert discover(harness.repo) is None


async def test_discover_ignores_an_engine_json_whose_socket_is_gone(harness):
    info = Path(harness.socket_path).with_name("engine.json")
    data = json.loads(info.read_text())
    data["socket"] = str(info.with_name("gone.sock"))
    info.write_text(json.dumps(data))
    assert discover(harness.repo) is None


async def test_the_engine_holds_the_repo_lock_while_it_serves(tmp_path):
    repo = tmp_path / "locked"
    repo.mkdir()
    engine = make_engine([worktree(str(repo))], repo_root=str(repo))
    await engine.start()
    lock = v1.socket_path(str(repo)).with_name("engine.lock")
    try:
        assert _mode(lock) == 0o600
        assert lock.read_text().strip() == str(os.getpid())
        assert runtime.EngineLock(lock).acquire() is False
    finally:
        await engine.stop()
    assert not lock.exists()
    assert not lock.parent.exists()  # the dir went with it


async def test_a_second_engine_loses_the_lock_and_names_the_first(tmp_path):
    repo = tmp_path / "twice"
    repo.mkdir()
    first = make_engine([worktree(str(repo))], repo_root=str(repo))
    second = make_engine([worktree(str(repo))], repo_root=str(repo))
    notices: list[dict] = []
    second.subscribe(
        lambda e: notices.append(e.data) if e.event == "engine.notice" else None
    )
    await first.start()
    try:
        await second.start()
        assert second.v1_socket_path is None
        assert second.other_instance == os.getpid()
        assert f"(pid {os.getpid()})" in notices[-1]["message"]
        await second.stop()
        # The loser touched nothing of the winner's.
        assert discover(str(repo)) == Path(first.v1_socket_path)
        assert v1.socket_path(str(repo)).with_name("engine.lock").exists()
    finally:
        await first.stop()


async def test_stale_files_of_a_dead_engine_are_replaced(tmp_path):
    """A killed engine leaves its socket, engine.json and engine.lock; the
    lock died with it, so the next engine takes over all three."""
    repo = tmp_path / "stale"
    repo.mkdir()
    path = v1.socket_path(str(repo))
    v1.private_dir(path.parent)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    path.with_name("engine.json").write_text(
        json.dumps({"pid": 999999, "socket": str(path), "repo_root": str(repo)})
    )
    path.with_name("engine.lock").write_text("999999\n")
    engine = make_engine([worktree(str(repo))], repo_root=str(repo))
    await engine.start()
    try:
        assert engine.v1_socket_path == str(path)
        info = json.loads(path.with_name("engine.json").read_text())
        assert info["pid"] == os.getpid()
        client = await Client.connect(str(path))
        assert client.hello.engine_id == engine.engine_id
        await client.close()
    finally:
        await engine.stop()


async def test_a_lock_on_an_unlinked_file_guards_nothing(tmp_path):
    """A contender that opened the file before the holder released (and
    unlinked) it can lock the old inode; the next acquire must not care."""
    path = tmp_path / "engine.lock"
    first = runtime.EngineLock(path)
    assert first.acquire()
    fd = os.open(path, os.O_RDWR)
    first.release()
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the old inode
    second = runtime.EngineLock(path)
    assert second.acquire()
    assert os.fstat(fd).st_ino != os.stat(path).st_ino
    os.close(fd)
    assert runtime.lock_holder(path) == os.getpid()
    second.release()
    assert not path.exists()
