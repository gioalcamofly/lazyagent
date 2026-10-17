"""What the contract suite needs from an engine under test.

The suite talks to the engine over its socket only, so any implementation
(this Python engine today, a Rust one later) passes it by providing a
:class:`Harness`: a running engine whose repo has two worktrees, ``repo``
(main) and ``feat``, whose agent provider runs tests/engine/fake_agent.py
(instruction = arguments), plus a way to connect over a given transport.

Hooks a black-box engine may not have (``emit``, ``local_resize``) raise
:class:`NotSupported`, and the tests that need them skip.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from lazyagent.engine.client import Client
from lazyagent.engine.server import V1Server, socket_path
from tests.engine.fakes import fake_agent_provider, make_engine, worktree


class NotSupported(Exception):
    pass


class Harness:
    """Base: an engine reachable at :attr:`socket_path`."""

    name = "abstract"
    repo: str
    feat: str
    socket_path: str

    def __init__(self, transport: str) -> None:
        self.transport = transport
        self._clients: list[Client] = []

    async def connect(self, *, hello: bool = True, client: str = "cli") -> Client:
        if self.transport != "unix":
            raise NotSupported(f"transport {self.transport}")
        conn = await Client.connect(self.socket_path, hello=hello, client=client)
        self._clients.append(conn)
        return conn

    async def open_raw(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A bare connection, for hand-made frames."""
        if self.transport != "unix":
            raise NotSupported(f"transport {self.transport}")
        return await asyncio.open_unix_connection(self.socket_path)

    async def close_clients(self) -> None:
        for conn in self._clients:
            await conn.close()
        self._clients.clear()

    async def start(self, tmp_path: Path) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        await self.close_clients()

    def emit(self, event: str, data: Any) -> None:
        """Make the engine push an arbitrary event."""
        raise NotSupported("emit")

    def local_resize(self, session_id: str, rows: int, cols: int) -> None:
        """Resize a session the way an in-process client would."""
        raise NotSupported("local_resize")

    def attach_ui(self):
        """Attach a stand-in hosted UI; returns its detach function."""
        raise NotSupported("attach_ui")

    def publish_view(self, view: dict[str, Any]) -> None:
        """Announce a view the way a hosted UI would."""
        raise NotSupported("publish_view")

    async def restart(self) -> None:
        """Stop the engine (its agents die with it) and start a new one for
        the same repo, which finds them in its restart manifest. Clients
        must connect again."""
        raise NotSupported("restart")

    def detach(self) -> list[int]:
        """Run the engine as a detached one would, except that a
        ``shutdown`` only counts: returns the list that grows by one each
        time the engine would stop."""
        raise NotSupported("detach")


class PythonHarness(Harness):
    """This package's Engine, with a fake git world and the fake agent."""

    name = "python"

    def __init__(self, transport: str, **server_options: Any) -> None:
        super().__init__(transport)
        self._server_options = server_options

    async def start(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        feat = tmp_path / "repo-feat"
        repo.mkdir()
        feat.mkdir()
        self.repo, self.feat = str(repo), str(feat)
        self._tmp = tmp_path
        self.socket_path = str(socket_path(self.repo))
        await self._start_engine()

    async def _start_engine(self, world=None) -> None:
        self.engine = make_engine(
            [worktree(self.repo), worktree(self.feat, "feat", is_main=False)],
            repo_root=self.repo,
            provider=fake_agent_provider(self._tmp),
            world=world,
        )
        self.engine.open_manifest(self._tmp / "manifest.json")
        await self.engine.start(ipc=False)
        self.server = V1Server(
            self.engine,
            self.socket_path,
            engine_id=self.engine.instance_id,
            **self._server_options,
        )
        assert await self.server.start()

    async def restart(self) -> None:
        await self.close_clients()
        await self.server.stop()
        await self.engine.stop()
        await self._start_engine(self.engine.world)

    async def stop(self) -> None:
        await super().stop()
        await self.server.stop()
        await self.engine.stop()

    def emit(self, event: str, data: Any) -> None:
        self.engine.events.emit(event, data)

    def local_resize(self, session_id: str, rows: int, cols: int) -> None:
        session = self.engine.find_session(session_id)
        assert session is not None
        session.resize(rows, cols)

    def attach_ui(self):
        return self.engine.attach_ui()

    def publish_view(self, view: dict[str, Any]) -> None:
        self.engine.publish_view(view)

    def detach(self) -> list[int]:
        calls: list[int] = []
        self.engine.shutdown_handler = lambda: calls.append(1)
        return calls


IMPLEMENTATIONS = {"python": PythonHarness}
TRANSPORTS = ("unix",)
