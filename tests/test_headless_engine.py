"""``python -m lazyagent.engine``: an engine with no TUI, and a TUI on it
(``LAZYAGENT_ENGINE=remote``) that never loads the engine.

Both run as real processes in a fresh git repo, with the test's runtime
dir.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

from lazyagent.engine.client import discover

_TUI = textwrap.dedent(
    """
    import asyncio, json, sys
    from lazyagent.app import LazyAgent

    async def main():
        app = LazyAgent(repo_path=sys.argv[1])
        async with app.run_test() as pilot:
            for _ in range(200):
                if app.backend.connected and app.worktrees:
                    break
                await asyncio.sleep(0.05)
            await pilot.pause()
            result = {
                "remote": app.backend.remote,
                "worktrees": [wt.path for wt in app.worktrees],
                "engine_loaded": "lazyagent.engine.engine" in sys.modules,
            }
            await pilot.press("q")
        print(json.dumps(result))

    asyncio.run(main())
    """
)


def _repo(tmp_path, env) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        [
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
    ):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True)
    return str(repo.resolve())


def _wait(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        time.sleep(0.05)


def test_a_remote_tui_on_a_headless_engine(tmp_path, runtime_dir):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": runtime_dir,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    env.pop("LAZYAGENT_ENGINE", None)
    env.pop("LAZYAGENT_ENGINE_SOCKET", None)
    root = _repo(tmp_path, env)
    engine = subprocess.Popen(
        [sys.executable, "-m", "lazyagent.engine", root],
        env=env,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait(lambda: discover(root, env) is not None)
        tui = subprocess.run(
            [sys.executable, "-c", _TUI, root],
            env={**env, "LAZYAGENT_ENGINE": "remote"},
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        assert tui.returncode == 0, tui.stderr
        result = json.loads(tui.stdout.strip().splitlines()[-1])
        assert result == {"remote": True, "worktrees": [root], "engine_loaded": False}
        assert engine.poll() is None  # quitting the TUI left the engine running
    finally:
        engine.send_signal(signal.SIGTERM)
        try:
            _, stderr = engine.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            engine.kill()
            raise
    assert engine.returncode == 0, stderr
    assert f"serving {root} on " in stderr
    assert discover(root, env) is None  # it cleaned up after itself


def test_a_second_engine_for_the_same_repo_refuses_to_start(tmp_path, runtime_dir):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_RUNTIME_DIR": runtime_dir,
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    root = _repo(tmp_path, env)
    first = subprocess.Popen(
        [sys.executable, "-m", "lazyagent.engine", root],
        env=env,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait(lambda: discover(root, env) is not None)
        second = subprocess.run(
            [sys.executable, "-m", "lazyagent.engine", root],
            env=env,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert second.returncode == 1
        assert "already serves this repo" in second.stderr
        assert first.poll() is None
    finally:
        first.send_signal(signal.SIGTERM)
        first.wait(timeout=20)
