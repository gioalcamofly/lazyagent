"""``lazyagent engine …`` and a remote TUI's auto-start, as real processes.

Each test gets a fresh git repo, its own runtime and state dirs, and a
``claude`` on PATH that runs tests/engine/fake_agent.py, so the engine's
real (Claude) provider spawns the fake agent. Every process a test starts
carries its runtime dir in its environment; the ``world`` fixture kills
whatever is left of them and fails the test if anything was.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import textwrap
import threading
import time
from pathlib import Path

import pytest

from lazyagent.engine.client import Client
from lazyagent.engine.manifest import manifest_path
from lazyagent.engine.runtime import log_path, runtime_dir
from tests.engine.fakes import FAKE_AGENT

_ANSI = re.compile(
    rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Za-z]|\x1b[=>]"
)


def _processes_with(marker: str) -> list[int]:
    """Pids of this user's processes whose environment contains ``marker``."""
    pids = []
    needle = marker.encode()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as f:
                if needle in f.read():
                    pids.append(int(entry))
        except OSError:
            continue
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie (exited, not yet reaped by its parent) is gone for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _wait(predicate, timeout: float = 20.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"{what} not met in time"
        time.sleep(0.02)


class World:
    """A repo, its env, and helpers to run lazyagent in it."""

    def __init__(self, tmp_path: Path, runtime: str) -> None:
        self.tmp = tmp_path
        self.runtime = runtime
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_AGENT} "$@"\n')
        claude.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": runtime,
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TERM": "xterm-256color",
        }
        for name in (
            "LAZYAGENT_ENGINE",
            "LAZYAGENT_ENGINE_SOCKET",
            "LAZYAGENT_ENGINE_AUTOSTART",
            "LAZYAGENT_HOST",
            "TMUX",
            "LAZYAGENT_SOCKET",
        ):
            self.env.pop(name, None)
        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (
            ["init", "-q", "-b", "main"],
            ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
             "--allow-empty", "-m", "init"],
        ):
            subprocess.run(["git", *args], cwd=repo, env=self.env, check=True)
        self.repo = str(repo.resolve())
        self.rundir = runtime_dir(self.repo, self.env)

    def lazyagent(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "lazyagent", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def status(self) -> dict:
        result = self.lazyagent("engine", "status", self.repo, "--json")
        assert result.returncode in (0, 3), result.stderr
        return json.loads(result.stdout)

    def engine_pids(self) -> list[int]:
        return [
            pid
            for pid in _processes_with(self.runtime)
            if _cmdline(pid)[1:3] == ["-m", "lazyagent.engine"] and _alive(pid)
        ]

    def info(self) -> dict:
        return json.loads((self.rundir / "engine.json").read_text())

    async def client(self) -> Client:
        return await Client.connect(self.rundir / "engine.sock", client="cli")

    def leftovers(self) -> list[int]:
        return [pid for pid in _processes_with(self.runtime) if _alive(pid)]

    def kill_all(self) -> list[int]:
        left = self.leftovers()
        for pid in left:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        return left


def _cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


@pytest.fixture
def world(tmp_path, runtime_dir):
    w = World(tmp_path, runtime_dir)
    yield w
    left = w.kill_all()
    assert left == [], f"processes left behind: {[_cmdline(p) for p in left]}"


class Tui:
    """``lazyagent`` (the real CLI) on a PTY of its own; a thread keeps
    reading its output, so it never blocks on a full terminal."""

    def __init__(self, world: World, *args: str, rows: int = 40, cols: int = 120):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lazyagent", *args],
            env=world.env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.master = master
        self.output = bytearray()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while True:
            try:
                readable, _, _ = select.select([self.master], [], [], 0.05)
                if not readable:
                    if self.proc.poll() is not None:
                        return
                    continue
                data = os.read(self.master, 65536)
            except (OSError, ValueError):
                return
            if not data:
                return
            with self._lock:
                self.output += data

    def text(self) -> str:
        with self._lock:
            data = bytes(self.output)
        return _ANSI.sub(b"", data).decode(errors="replace")

    def wait_text(self, needle: str, timeout: float = 20.0) -> float:
        """Seconds from launch until ``needle`` is on the screen."""
        deadline = time.monotonic() + timeout
        while needle not in self.text():
            assert time.monotonic() < deadline, (
                f"{needle!r} not shown; got: {self.text()[-2000:]}"
            )
            time.sleep(0.005)
        return time.monotonic() - self.started

    def send(self, data: bytes) -> None:
        os.write(self.master, data)

    def quit(self, timeout: float = 20.0) -> int:
        self.send(b"q")
        return self.proc.wait(timeout)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._reader.join(5)
        os.close(self.master)


@contextlib.contextmanager
def tui(world: World, *args: str):
    t = Tui(world, *args)
    try:
        yield t
    finally:
        t.close()


def _legacy_call(path: str, method: str, **params) -> dict:
    """One NDJSON request on the legacy IPC socket, as the MCP server does."""
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(10)
        sock.connect(path)
        sock.sendall(
            json.dumps({"id": "1", "method": method, "params": params}).encode() + b"\n"
        )
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            assert chunk, "the IPC server closed the connection"
            data += chunk
    response = json.loads(data)
    assert "error" not in response, response
    return response["result"]


async def _wait_screen(
    client: Client, path: str, agent_id: str, needle: str
) -> list[str]:
    for _ in range(500):
        result = await client.request(
            "read_agent_output", worktree_path=path, agent_id=agent_id, lines=50
        )
        lines = result["lines"]
        if any(needle in line for line in lines):
            return lines
        await asyncio.sleep(0.02)
    raise AssertionError(f"{needle!r} never shown: {lines}")


def _fake_agents(world: World) -> list[int]:
    return [
        pid
        for pid in world.leftovers()
        if any(arg.endswith("fake_agent.py") for arg in _cmdline(pid))
    ]


# ----------------------------------------------------------------------
# lazyagent engine start / status / stop / logs
# ----------------------------------------------------------------------


def test_start_status_stop(world):
    assert world.status() == {"running": False, "repo_root": world.repo}

    started = world.lazyagent("engine", "start", world.repo)
    assert started.returncode == 0, started.stderr
    status = world.status()
    assert status["running"] and status["detached"]
    [pid] = world.engine_pids()
    assert status["pid"] == pid == world.info()["pid"]
    assert f"started for {world.repo} (pid {pid})" in started.stdout
    # Detached: a session (and process group) of its own, no terminal.
    assert os.getsid(pid) == pid
    assert os.readlink(f"/proc/{pid}/fd/0") == "/dev/null"
    log = log_path(world.repo, world.env)
    assert os.readlink(f"/proc/{pid}/fd/2") == str(log)
    assert "serving " + world.repo in log.read_text()

    again = world.lazyagent("engine", "start", world.repo)
    assert again.returncode == 0
    assert f"already running for {world.repo} (pid {pid}, detached)" in again.stdout
    assert world.engine_pids() == [pid]

    logs = world.lazyagent("engine", "logs", world.repo, "-n", "5")
    assert "serving " + world.repo in logs.stdout

    stopped = world.lazyagent("engine", "stop", world.repo)
    assert stopped.returncode == 0, stopped.stderr
    assert f"stopped (pid {pid})" in stopped.stdout
    assert not _alive(pid)
    assert world.status() == {"running": False, "repo_root": world.repo}
    assert not world.rundir.exists()  # socket, engine.json, lock and dir gone
    assert "shutdown requested by a client" in log.read_text()

    assert world.lazyagent("engine", "status", world.repo).returncode == 3
    assert "no engine running" in world.lazyagent("engine", "stop", world.repo).stdout


def test_two_starts_make_one_engine(world):
    starts = [
        subprocess.Popen(
            [sys.executable, "-m", "lazyagent", "engine", "start", world.repo],
            env=world.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    outputs = [p.communicate(timeout=60) for p in starts]
    assert [p.returncode for p in starts] == [0, 0, 0], outputs
    _wait(lambda: len(world.engine_pids()) == 1, what="one engine")
    [pid] = world.engine_pids()
    for out, _err in outputs:
        assert f"(pid {pid}" in out
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0


def test_a_killed_engine_leaves_stale_files_the_next_start_replaces(world):
    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    [pid] = world.engine_pids()
    os.kill(pid, signal.SIGKILL)
    _wait(lambda: not _alive(pid), what="engine killed")
    # It could not clean up: socket, engine.json and lock file stay.
    for name in ("engine.sock", "engine.json", "engine.lock"):
        assert (world.rundir / name).exists()
    assert world.status()["running"] is False  # nobody listens: not running

    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    [new] = world.engine_pids()
    assert new != pid and world.info()["pid"] == new
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0


def test_stop_stops_the_agents_and_if_idle_refuses_while_one_runs(world):
    assert world.lazyagent("engine", "start", world.repo).returncode == 0

    async def spawn() -> dict:
        client = await world.client()
        try:
            agent = await client.request(
                "spawn_agent", worktree_path=world.repo, env=world.env
            )
            await _wait_screen(client, world.repo, agent["agent_id"], "your turn>")
            return agent
        finally:
            await client.close()

    asyncio.run(spawn())
    [agent] = _fake_agents(world)
    refused = world.lazyagent("engine", "stop", world.repo, "--if-idle")
    assert refused.returncode == 1
    assert "1 agent still running" in refused.stderr
    assert _alive(agent) and world.status()["running"]

    stopped = world.lazyagent("engine", "stop", world.repo)
    assert stopped.returncode == 0, stopped.stderr
    assert "1 agent stopped" in stopped.stdout
    _wait(lambda: not _alive(agent), what="agent stopped")


def test_an_engine_inside_a_tui_is_not_stopped_from_the_cli(world):
    with tui(world, world.repo, "--embedded") as t:
        t.wait_text("main")
        _wait(lambda: (world.rundir / "engine.json").exists(), what="v1 socket")
        status = world.status()
        assert status["running"] and status["detached"] is False
        refused = world.lazyagent("engine", "stop", world.repo)
        assert refused.returncode == 1
        assert "inside a lazyagent TUI" in refused.stderr
        # Nor does a second engine start next to it.
        started = world.lazyagent("engine", "start", world.repo)
        assert "inside a lazyagent TUI" in started.stdout
        assert world.engine_pids() == []
        assert t.quit() == 0


# ----------------------------------------------------------------------
# A remote TUI: auto-start (K8), quit, reattach
# ----------------------------------------------------------------------


def test_cold_start_auto_starts_the_engine_and_agents_outlive_the_tui(world):
    """K8, the unattended boot: no engine; ``lazyagent --remote`` in a
    terminal starts one, and an agent spawned over MCP (the legacy socket,
    as /gio-planner's orchestrator does) runs on it, keeps running when the
    TUI quits, and is still reachable over MCP afterwards."""
    assert world.engine_pids() == []
    with tui(world, world.repo, "--remote") as t:
        t.wait_text("Started an engine")
        [pid] = world.engine_pids()
        assert os.getsid(pid) == pid  # detached from the TUI's session
        # The engine owns the legacy socket the agents' MCP servers use.
        ipc = f"/tmp/lazyagent-{pid}/ipc.sock"
        agent = _legacy_call(ipc, "spawn_agent", worktree_path=world.repo)
        _wait(lambda: _fake_agents(world) != [], what="the agent")
        t.wait_text("running")  # the sidebar has it, through the engine
        [fake] = _fake_agents(world)
        command = " ".join(_cmdline(fake))
        assert "--mcp-config" in command
        config = command.split("--mcp-config")[1].split()[0]
        servers = json.loads(Path(config).read_text())["mcpServers"]
        [server] = servers.values()
        assert server["env"]["LAZYAGENT_SOCKET"] == ipc
        assert t.quit() == 0

    assert world.engine_pids() == [pid] and _alive(fake)
    listed = _legacy_call(ipc, "list_agents", worktree_path=world.repo)
    assert [a["agent_id"] for a in listed["agents"]] == [agent["agent_id"]]
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0
    _wait(lambda: not _alive(fake), what="agent stopped with the engine")
    assert not os.path.exists(ipc)


_ATTACH = textwrap.dedent(
    """
    import asyncio, json, sys
    from lazyagent.app import LazyAgent
    from lazyagent.backend import make_backend
    from lazyagent.widgets.worktree_list import WorktreeList, WorktreeListItem

    async def main():
        repo, agent_id, needle = sys.argv[1:4]
        app = LazyAgent(repo_path=repo, backend=make_backend(repo))
        async with app.run_test(size=(120, 40)) as pilot:
            backend = app.backend
            for _ in range(500):
                if backend.connected and agent_id in backend.agent_ids(repo):
                    break
                await asyncio.sleep(0.02)
            # Show the main worktree (the orchestrator is selected at start),
            # so the agent's tab is on screen and streams.
            worktrees = app.query_one(WorktreeList)
            [item] = [
                c for c in worktrees.children if isinstance(c, WorktreeListItem)
            ]
            worktrees.index = 1
            highlighted = WorktreeList.Highlighted(worktrees, item)
            await app.on_list_view_highlighted(highlighted)
            session = await backend.session(repo, agent_id)
            for _ in range(500):
                lines = [line.rstrip() for line in session.screen.display]
                if session.attaches and any(needle in line for line in lines):
                    break
                await pilot.pause(0.02)
            await pilot.pause(0.2)  # let a size claim settle
            engine = await backend.request(
                "read_agent_output", worktree_path=repo, agent_id=agent_id, lines=200
            )
            result = {
                "engine_id": backend.engine_id,
                "agents": backend.agent_ids(repo),
                "attaches": session.attaches,
                "size": [session.rows, session.cols],
                "screen": [line.rstrip() for line in session.screen.display],
                "engine_screen": engine["lines"][-session.rows:],
                "engine_loaded": "lazyagent.engine.engine" in sys.modules,
            }
            await pilot.press("q")
        print(json.dumps(result))

    asyncio.run(main())
    """
)


def _attach_tui(world: World, agent_id: str, needle: str) -> dict:
    """A remote TUI (Textual's test driver, in its own process) shows the
    agent until ``needle`` is on its screen, then quits; what it saw."""
    result = subprocess.run(
        [sys.executable, "-c", _ATTACH, world.repo, agent_id, needle],
        env={**world.env, "LAZYAGENT_ENGINE": "remote"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_new_tui_reattaches_with_the_agent_screen(world):
    """An agent shown in a TUI keeps working after that TUI quits; the next
    TUI comes back to the same engine and its screen (a snapshot) matches
    the engine's, line for line."""
    with tui(world, world.repo, "--remote") as t:
        t.wait_text("Started an engine")
        assert t.quit() == 0
    [pid] = world.engine_pids()
    engine_id = world.info()["engine_id"]

    async def call(method: str, **params):
        client = await world.client()
        try:
            return await client.request(method, **params)
        finally:
            await client.close()

    agent = asyncio.run(call("spawn_agent", worktree_path=world.repo, env=world.env))
    agent_id = agent["agent_id"]
    first = _attach_tui(world, agent_id, "your turn>")
    assert first["engine_id"] == engine_id and first["agents"] == [agent_id]
    assert first["engine_loaded"] is False

    # With no TUI attached, the agent goes on.
    asyncio.run(
        call("send_agent_input", worktree_path=world.repo, agent_id=agent_id, text="hi")
    )

    second = _attach_tui(world, agent_id, "got: 'hi'")
    assert second["engine_id"] == engine_id
    assert second["attaches"] == ["snapshot"]
    assert second["size"] == first["size"]  # sized by the first TUI, kept
    assert second["screen"] == second["engine_screen"]
    assert "got: 'hi' paste=False" in second["screen"]
    assert world.engine_pids() == [pid]
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0


def test_autostart_failure_keeps_the_notice(world):
    """An engine that cannot start: the TUI opens anyway and says why."""
    # A file where the runtime dir must go makes every engine fail.
    world.rundir.parent.mkdir(parents=True, exist_ok=True)
    world.rundir.write_text("in the way")
    with tui(world, world.repo, "--remote") as t:
        t.wait_text("could not start an engine")
        assert t.quit() == 0
    assert world.engine_pids() == []
    world.rundir.unlink()


def test_a_tui_does_not_restart_an_engine_stopped_on_purpose(world):
    with tui(world, world.repo, "--remote") as t:
        t.wait_text("Started an engine")
        [pid] = world.engine_pids()
        assert world.lazyagent("engine", "stop", world.repo).returncode == 0
        t.wait_text("it was stopped with")
        time.sleep(0.5)
        assert world.engine_pids() == []
        assert t.quit() == 0
    assert world.engine_pids() == [] and not _alive(pid)


# ----------------------------------------------------------------------
# The restart manifest: crash, restart, restore
# ----------------------------------------------------------------------


def test_agents_of_a_crashed_engine_are_restored_into_their_conversation(world):
    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    [pid] = world.engine_pids()

    async def spawn() -> dict:
        client = await world.client()
        try:
            agent = await client.request(
                "spawn_agent", worktree_path=world.repo, env=world.env
            )
            await _wait_screen(client, world.repo, agent["agent_id"], "your turn>")
            return agent
        finally:
            await client.close()

    agent = asyncio.run(spawn())
    [fake] = _fake_agents(world)
    # What Claude's hooks would log: the conversation id the restore resumes.
    args = _cmdline(fake)
    hook_log = Path(args[args.index("--settings") + 1]).with_name("hook-events.jsonl")
    conversation = "5d3c2b1a-0f9e-4d8c-b7a6-112233445566"
    event = {"hook_event_name": "UserPromptSubmit", "session_id": conversation}
    with hook_log.open("a") as f:
        f.write(json.dumps(event) + "\n")
    path = manifest_path(world.repo, world.env)
    _wait(
        lambda: path.exists() and conversation in path.read_text(),
        what="the manifest noting it",
    )

    os.kill(pid, signal.SIGKILL)  # a crash: no stop, no last write
    _wait(lambda: not _alive(pid) and not _alive(fake), what="engine and agent dead")

    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    status = world.status()
    assert status["agents"] == [] and status["restorable"] == 1
    listed = world.lazyagent("engine", "restore", world.repo, "--json")
    [entry] = json.loads(listed.stdout)
    assert entry["agent_id"] == agent["agent_id"]
    assert entry["conversation_id"] == conversation

    restored = world.lazyagent("engine", "restore", world.repo, "--all")
    assert restored.returncode == 0, restored.stderr
    assert f"restored {agent['agent_id']} in {world.repo} (conversation)" in (
        restored.stdout
    )
    _wait(lambda: len(_fake_agents(world)) == 1, what="the agent back")
    [back] = _fake_agents(world)
    command = " ".join(_cmdline(back))
    assert f"--resume {conversation}" in command
    assert f"--name {entry['session_name']}" in command and entry['session_name']
    assert world.status()["agents"][0]["agent_id"] == agent["agent_id"]

    # A deliberate stop keeps them restorable too; dismissing forgets them.
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0
    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    assert world.status()["restorable"] == 1
    dismissed = world.lazyagent("engine", "restore", world.repo, "--all", "--dismiss")
    assert "dismissed 1" in dismissed.stdout
    assert world.status()["restorable"] == 0
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0


# ----------------------------------------------------------------------
# lazyagent engine restart
# ----------------------------------------------------------------------


def _note_conversation(world: World, fake: int, conversation: str) -> None:
    """What Claude's hooks would log for the fake agent ``fake``."""
    args = _cmdline(fake)
    hook_log = Path(args[args.index("--settings") + 1]).with_name("hook-events.jsonl")
    event = {"hook_event_name": "UserPromptSubmit", "session_id": conversation}
    with hook_log.open("a") as f:
        f.write(json.dumps(event) + "\n")
    path = manifest_path(world.repo, world.env)
    _wait(
        lambda: path.exists() and conversation in path.read_text(),
        what="the manifest noting it",
    )


def _spawn(world: World, **params) -> dict:
    async def main() -> dict:
        client = await world.client()
        try:
            agent = await client.request("spawn_agent", env=world.env, **params)
            key = params.get("worktree_path", "__orchestrator__")
            await _wait_screen(client, key, agent["agent_id"], "your turn>")
            return agent
        finally:
            await client.close()

    return asyncio.run(main())


def test_restart_restores_exactly_the_agents_it_stopped(world):
    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    # An entry left by an earlier engine: still offered after the restart.
    _spawn(world, worktree_path=world.repo)
    [old] = _fake_agents(world)
    _note_conversation(world, old, "0ld00000-0000-4000-8000-000000000000")
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0
    assert world.lazyagent("engine", "start", world.repo).returncode == 0
    assert world.status()["restorable"] == 1

    conversations = {"agent": "a9e00000-0000-4000-8000-000000000001",
                     "orchestrator": "0c000000-0000-4000-8000-000000000002"}
    agent = _spawn(world, worktree_path=world.repo, resume_mode="new")
    [fake] = _fake_agents(world)
    _note_conversation(world, fake, conversations["agent"])
    orchestrator = _spawn(world, kind="orchestrator")
    assert orchestrator["agent_id"] == ""
    [fake] = set(_fake_agents(world)) - {fake}
    _note_conversation(world, fake, conversations["orchestrator"])
    old_pid = world.info()["pid"]

    result = world.lazyagent("engine", "restart", world.repo, "--restore", "--json")
    assert result.returncode == 0, result.stderr
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["step"] for e in lines[:-1]] == ["stopping", "starting", "started", "restoring"]
    done = lines[-1]["result"]
    assert done["error"] is None and done["failed"] == []
    assert done["stopped"] == 2 and done["old_pid"] == old_pid != done["pid"]
    assert sorted(a["kind"] for a in done["restored"]) == ["agent", "orchestrator"]
    assert done["left"] == 1, "the older entry stays offered, not restored"

    _wait(lambda: len(_fake_agents(world)) == 2, what="both back")
    commands = [" ".join(_cmdline(pid)) for pid in _fake_agents(world)]
    assert all("--continue" not in c for c in commands), commands
    assert any(f"--resume {conversations['agent']}" in c for c in commands)
    assert any(f"--resume {conversations['orchestrator']}" in c for c in commands)
    status = world.status()
    # The orchestrator comes back as the orchestrator, the agent as itself.
    ids = {(a["worktree_path"], a["agent_id"]) for a in status["agents"]}
    assert ids == {(None, ""), (world.repo, agent["agent_id"])}
    assert status["restorable"] == 1

    # Without --restore they all stay restorable; with no engine, nothing is.
    plain = world.lazyagent("engine", "restart", world.repo)
    assert plain.returncode == 0, plain.stderr
    assert "3 agent(s) to restore" in plain.stdout
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0
    cold = world.lazyagent("engine", "restart", world.repo, "--restore", "--json")
    assert json.loads(cold.stdout.splitlines()[-1])["result"]["restored"] == []
    assert world.status()["restorable"] == 3
    assert world.lazyagent("engine", "stop", world.repo).returncode == 0


def test_restart_refuses_an_engine_inside_a_tui(world):
    with tui(world, world.repo, "--embedded") as t:
        t.wait_text("main")
        _wait(lambda: (world.rundir / "engine.json").exists(), what="v1 socket")
        result = world.lazyagent("engine", "restart", world.repo, "--json")
        assert result.returncode == 1
        done = json.loads(result.stdout.splitlines()[-1])["result"]
        assert "inside a lazyagent TUI" in done["error"]
        assert world.engine_pids() == []
        assert t.quit() == 0
