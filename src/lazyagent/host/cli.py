"""``lazyagent host [repo] [--port N] [--open]``."""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

from lazyagent.engine.client import Client, ConnectionClosed, RequestError, discover
from lazyagent.host.server import HostServer
from lazyagent.host.tui import HostedTui
from lazyagent.protocol.hosted import HOST_ENV

STATIC_ROOT = Path(__file__).parent / "static"
# How long to wait for the TUI to say where its engine is.
DISCOVERY_TIMEOUT = 30.0


def repo_root(path: str) -> str:
    """``git rev-parse --show-toplevel`` for ``path``: the string the
    engine's runtime dir is named after."""
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or f"{path} is not a git repository")
    return result.stdout.strip()


def tui_env(host_id: str, environ: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    # The TUI runs on our PTY, not inside the tmux the host may run in.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env[HOST_ENV] = host_id
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    return env


class Host:
    """The TUI on a PTY plus the web server, until either goes away."""

    def __init__(
        self,
        *,
        repo: str | None,
        cwd: str,
        root: str,
        port: int = 0,
        open_browser: bool = False,
        tui_argv: list[str] | None = None,
        environ: dict[str, str] | None = None,
        out=sys.stderr,
    ) -> None:
        self.root = root
        self.host_id = f"lazyagent-host-{os.getpid()}"
        self.open_browser = open_browser
        self.out = out
        argv = tui_argv or [sys.executable, "-m", "lazyagent"]
        if repo is not None:
            argv = [*argv, repo]
        self.tui = HostedTui(
            argv,
            cwd=cwd,
            env=tui_env(self.host_id, environ),
            on_output=self._on_output,
            on_announce=self._on_announce,
        )
        self.server = HostServer(
            self.tui,
            token=secrets.token_urlsafe(32),
            static_root=STATIC_ROOT,
            repo_root=root,
            host_id=self.host_id,
            port=port,
        )
        self._announced: asyncio.Future[str | None] | None = None
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()

    def _print(self, message: str) -> None:
        print(f"lazyagent host: {message}", file=self.out, flush=True)

    def _on_output(self, data: bytes) -> None:
        self.server.broadcast_output(data)

    def _on_announce(self, payload: dict) -> None:
        if self._announced is not None and not self._announced.done():
            socket = payload.get("socket")
            self._announced.set_result(socket if isinstance(socket, str) else None)

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._announced = loop.create_future()
        await self.server.start()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(signum, self.stop)
        try:
            await self.tui.start()
            self._print(f"lazyagent (pid {self.tui.pid}) is running in {self.root}")
            self._print(f"open {self.server.url}")
            self._print("Ctrl+C here, or q in the TUI, quits")
            if self.open_browser:
                webbrowser.open(self.server.url)
            self._spawn(self._discover())
            exited = loop.create_task(self.tui.wait())
            stopped = loop.create_task(self._stop.wait())
            await asyncio.wait({exited, stopped}, return_when=asyncio.FIRST_COMPLETED)
            stopped.cancel()
            if not exited.done():
                self._print("stopping lazyagent…")
                await self.tui.stop()
            code = await exited
            self._print(f"lazyagent exited ({code})")
            await self.server.tui_exited(code)
            return code
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                loop.remove_signal_handler(signum)
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            if self.tui.running:
                await self.tui.stop(grace=0)
            await self.server.close()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _discover(self) -> None:
        """Find the engine socket: the TUI's OSC 7717, else ``engine.json``."""
        assert self._announced is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DISCOVERY_TIMEOUT
        socket: str | None = None
        while loop.time() < deadline:
            try:
                socket = await asyncio.wait_for(asyncio.shield(self._announced), 0.5)
                break
            except asyncio.TimeoutError:
                found = discover(self.root)
                if found is not None:
                    socket = str(found)
                    break
        if socket is None:
            self._print("no engine socket found: the sidebar stays empty")
            return
        self.server.set_engine(socket)
        await self._watch_engine(socket)

    async def _watch_engine(self, socket: str) -> None:
        """The host's own engine connection: PR info for every worktree
        (``set_interest``), and engine notices on stderr."""
        try:
            client = await Client.connect(socket, client="host")
        except (OSError, ConnectionClosed, asyncio.TimeoutError) as e:
            self._print(f"cannot connect to the engine: {e}")
            return
        try:
            state = await client.request("get_sidebar_state")
            await client.request(
                "subscribe",
                since=state["cursor"],
                events=["worktrees.changed", "engine.notice"],
            )
            await self._set_interest(client, state["worktrees"])
            async for event in client.events():
                if event.event == "worktrees.changed":
                    await self._set_interest(client, event.data)
                elif event.event == "engine.notice":
                    self._print(f"{event.data['severity']}: {event.data['message']}")
        except (ConnectionClosed, RequestError):
            pass
        finally:
            await client.close()

    @staticmethod
    async def _set_interest(client: Client, worktrees: list[dict]) -> None:
        await client.request(
            "set_interest", worktree_paths=[w["path"] for w in worktrees]
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lazyagent host",
        description=(
            "Run lazyagent in a minimal web host (experimental): the TUI in a "
            "browser terminal next to a live sidebar. Serves 127.0.0.1 only."
        ),
    )
    parser.add_argument(
        "repo", nargs="?", default=None, help="Path to git repository (default: cwd)"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="Port on 127.0.0.1 (default: a free one)"
    )
    parser.add_argument(
        "--open", action="store_true", help="Open the page in the default browser"
    )
    args = parser.parse_args(argv)
    if not (STATIC_ROOT / "index.html").is_file():
        print(f"lazyagent host: web assets missing from {STATIC_ROOT}", file=sys.stderr)
        sys.exit(1)
    try:
        root = repo_root(args.repo or os.getcwd())
    except (ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    host = Host(
        repo=args.repo,
        cwd=os.getcwd(),
        root=root,
        port=args.port,
        open_browser=args.open,
    )
    try:
        code = asyncio.run(host.run())
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    # A TUI killed by a signal: the shell convention.
    sys.exit(code if code >= 0 else 128 - code)
