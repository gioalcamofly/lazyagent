"""Fixtures for the host tests that run the real TUI."""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from lazyagent.host.cli import Host, repo_root
from tests.engine.fakes import FAKE_AGENT


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path, runtime_dir, monkeypatch):
    """A repo with one extra worktree, and an environment that keeps the
    TUI's files (state, runtime dir, legacy socket, shell profile) out of
    the user's."""
    main = tmp_path / "repo"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    (main / "README").write_text("hi\n")
    _git("add", ".", cwd=main)
    _git("commit", "-qm", "init", cwd=main)
    _git("worktree", "add", "-q", str(tmp_path / "repo-feat"), "-b", "feat", cwd=main)
    # The legacy ipc.sock goes under $TMPDIR, and a Unix socket path must
    # stay short: next to the runtime dir, not under tmp_path.
    tmp = Path(f"{runtime_dir}-tmp")
    tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("LAZYAGENT_HOST", "TMUX", "LAZYAGENT_LOG", "LAZYAGENT_V1_SERVER"):
        monkeypatch.delenv(name, raising=False)
    return main


@pytest_asyncio.fixture
async def host(repo):
    h = Host(repo=None, cwd=str(repo), root=repo_root(str(repo)), out=io.StringIO())
    h.announcements = []  # type: ignore[attr-defined]
    task = asyncio.get_running_loop().create_task(h.run())
    for _ in range(500):
        if h.server.port and h.tui.pid:
            break
        await asyncio.sleep(0.01)
    tui_announce = h.tui.on_announce

    def record(payload: dict) -> None:
        h.announcements.append(payload)  # type: ignore[attr-defined]
        tui_announce(payload)

    h.tui.on_announce = record
    h.task = task  # type: ignore[attr-defined]
    try:
        yield h
    finally:
        if not task.done():
            h.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 30)
        await _assert_no_orphans(h)


def _processes_of(host_id: str) -> list[int]:
    """Pids whose environment carries this host's LAZYAGENT_HOST: the TUI,
    and everything it started (shells, agents)."""
    needle = f"LAZYAGENT_HOST={host_id}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
            state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if state != "Z" and needle in environ.split(b"\0"):
            found.append(int(entry.name))
    return found


async def _assert_no_orphans(host: Host) -> None:
    for _ in range(100):
        left = _processes_of(host.host_id)
        if not left:
            return
        await asyncio.sleep(0.05)
    for pid in left:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"processes left behind: {left}")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """``claude`` on the TUI's PATH is the scripted fake agent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_AGENT} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return shim
