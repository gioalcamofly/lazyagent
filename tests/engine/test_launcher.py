"""The launcher's pieces that need no engine: the log cap, start failures,
finding a live engine."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap

import pytest

from lazyagent.engine import launcher, runtime


def test_rotate_log_moves_a_full_log_aside(tmp_path):
    log = tmp_path / "engine.log"
    log.write_bytes(b"x" * 101)
    (tmp_path / "engine.log.1").write_text("older")
    launcher.rotate_log(log, max_bytes=100)
    assert not log.exists()
    assert (tmp_path / "engine.log.1").read_bytes() == b"x" * 101
    small = tmp_path / "small.log"
    small.write_text("ok")
    launcher.rotate_log(small, max_bytes=100)
    assert small.read_text() == "ok"
    launcher.rotate_log(tmp_path / "missing.log")  # no error


def test_tail(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    assert launcher.tail(log, 2) == "line 98\nline 99"
    assert launcher.tail(tmp_path / "missing") == ""


_ROTATING = textwrap.dedent(
    """
    import logging, os, sys
    from lazyagent.engine.headless import _StdioRotatingHandler

    path = sys.argv[1]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    handler = _StdioRotatingHandler(path, maxBytes=500, backupCount=1)
    log = logging.getLogger("t")
    log.addHandler(handler)
    for i in range(100):
        log.warning("message %03d %s", i, "." * 40)
    os.write(2, b"written to stderr after the rotations\\n")
    """
)


def test_the_engine_log_is_capped_and_stderr_follows_it(tmp_path):
    """Whatever the engine logs, the log and its one old file stay near the
    cap, and a traceback written to stderr later lands in the current file."""
    path = tmp_path / "engine.log"
    subprocess.run(
        [sys.executable, "-c", _ROTATING, str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    old = tmp_path / "engine.log.1"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["engine.log", "engine.log.1"]
    assert path.stat().st_size < 600 and old.stat().st_size < 600
    assert "written to stderr after the rotations" in path.read_text()
    assert "message 099" in path.read_text()


def test_a_start_that_fails_says_why_and_where_the_log_is(tmp_path, runtime_dir):
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": runtime_dir,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    repo = str(tmp_path / "not-a-repo")
    os.mkdir(repo)
    launch = launcher.spawn(repo, env)
    with pytest.raises(launcher.EngineStartError) as e:
        launch.wait(timeout=30)
    message = str(e.value)
    assert "it exited with code 1" in message
    assert f"(log: {runtime.log_path(repo, env)})" in message
    assert "Not a git repository" in message  # the log's tail
    assert launch.proc.poll() == 1


def test_find_needs_a_live_socket(tmp_path, runtime_dir):
    env = {"XDG_RUNTIME_DIR": runtime_dir}
    repo = "/some/repo"
    rundir = runtime.runtime_dir(repo, env)
    rundir.mkdir(parents=True)
    path = rundir / "engine.sock"
    (rundir / "engine.json").write_text(
        json.dumps({"socket": str(path), "repo_root": repo})
    )
    assert launcher.find(repo, env) is None  # no socket file
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    assert launcher.find(repo, env) is None  # nobody listens
    server.listen()
    try:
        assert launcher.find(repo, env) == path
        assert launcher.find("/other/repo", env) is None
    finally:
        server.close()
