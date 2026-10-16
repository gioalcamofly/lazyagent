"""The TUI's pilot tests, run against both backends (K9).

``backend_kind`` (a fixture in tests/conftest.py) is ``"inprocess"`` or
``"remote"``; :func:`tui_app` builds the app for it around an
:class:`~lazyagent.engine.engine.Engine` the test can reach as
``app.backend.engine`` either way:

- in-process: :class:`~lazyagent.backend.inprocess.InProcessBackend`, as
  ``lazyagent`` runs by default;
- remote: :class:`ServedEngineBackend`, a real
  :class:`~lazyagent.backend.remote.RemoteBackend` talking v1 over the
  engine's socket (in the test's runtime dir) to an engine on the same
  loop, which starts and stops with the app so the test's lifecycle is the
  same as in-process.

After acting on the engine directly, a test waits with :func:`settle`: in
remote mode the app learns about it through the socket.
"""

from __future__ import annotations

from lazyagent.app import LazyAgent
from lazyagent.backend import Backend
from lazyagent.backend.inprocess import InProcessBackend
from lazyagent.backend.remote import RemoteBackend
from lazyagent.engine.engine import Engine

BACKENDS = ("inprocess", "remote")


class ServedEngineBackend(RemoteBackend):
    """A :class:`RemoteBackend` whose engine runs on this loop and has the
    backend's lifetime (tests only: the real one never owns its engine)."""

    def __init__(self, engine: Engine, **options) -> None:
        options.setdefault("retry_initial", 0.05)
        options.setdefault("retry_max", 0.2)
        super().__init__(engine.repo_path, **options)
        self.engine = engine

    async def start(self) -> None:
        await self.engine.start()
        assert self.engine.v1_socket_path is not None
        self._socket_option = self.engine.v1_socket_path
        await super().start()
        assert self.connected

    async def stop(self) -> None:
        await super().stop()
        await self.engine.stop()


def make_backend(kind: str, engine: Engine) -> Backend:
    if kind == "inprocess":
        return InProcessBackend(engine)
    if kind == "remote":
        return ServedEngineBackend(engine)
    raise ValueError(kind)


def tui_app(
    kind: str, *, engine: Engine | None = None, repo_path: str = "/repo"
) -> LazyAgent:
    """A LazyAgent on ``engine`` (default: ``Engine(repo_path)``, which the
    caller has usually pointed at a fake worktree manager)."""
    engine = engine if engine is not None else Engine(repo_path)
    return LazyAgent(repo_path=repo_path, backend=make_backend(kind, engine))


async def settle(pilot) -> None:
    """Let what the engine just did reach the screen: a pause in-process; in
    remote mode, the events over the socket and what the app does with them
    (mounting a tab asks the engine for the terminal's size)."""
    app = pilot.app
    rounds = 2 if app.backend.remote else 1
    for _ in range(rounds):
        await app.backend.sync()
        await pilot.pause()
