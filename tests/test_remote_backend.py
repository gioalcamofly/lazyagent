"""What only the remote backend does: terminals fed by v1 streams, the
client store, losing and regaining the engine, and agents that outlive the
TUI.

The engine runs on the test's loop (real PTYs: the fake agent and bash),
serving v1 in the test's runtime dir; the TUI or a bare
:class:`RemoteBackend` connects to it over the socket like any client.
The pilot tests shared with the in-process backend run through
``backend_kind`` (tests/tui_backends.py).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from lazyagent.app import LazyAgent
from lazyagent.backend import make_backend
from lazyagent.backend.inprocess import InProcessBackend
from lazyagent.backend.remote import RemoteBackend
from lazyagent.engine.client import Client
from lazyagent.models import GitStatus
from lazyagent.widgets.center_panel import CenterPanel
from tests.contract.helpers import text_lines, until
from tests.engine.fakes import fake_agent_provider, make_engine, worktree

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]

SIZE = (160, 50)


def _engine(tmp_path: Path):
    repo, feat = tmp_path / "repo", tmp_path / "repo-feat"
    repo.mkdir(exist_ok=True)
    feat.mkdir(exist_ok=True)
    return make_engine(
        [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
        repo_root=str(repo),
        provider=fake_agent_provider(tmp_path),
    )


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    # The worktree shells are real `bash -l`: keep the user's profile out.
    monkeypatch.setenv("HOME", str(tmp_path))
    engine = _engine(tmp_path)
    await engine.start()
    assert engine.v1_socket_path is not None
    yield engine
    await engine.stop()


def _remote(engine, **options) -> RemoteBackend:
    options.setdefault("retry_initial", 0.05)
    options.setdefault("retry_max", 0.2)
    return RemoteBackend(engine.repo_path, socket_path=engine.v1_socket_path, **options)


@pytest_asyncio.fixture
async def backend(engine):
    backend = _remote(engine)
    await backend.start()
    assert backend.connected
    yield backend
    await backend.stop()


def _feat(engine) -> str:
    return engine.worktrees[1].path


async def _spawn(engine, *args: str):
    """A fake agent in the feat worktree, at its prompt."""
    info = await engine.spawn_agent(_feat(engine), " ".join(args) or None)
    session = engine.get_session(info.worktree_path, info.agent_id)
    await _engine_shows(session, "your turn>")
    return info, session


async def _engine_shows(session, text: str) -> None:
    def shows() -> bool:
        session.flush()
        return text in session.screen_text()

    await until(shows)


def _same_screen(remote, session) -> bool:
    """The remote display shows what the engine's screen does (a snapshot
    carries the last 2,000 scrollback lines, the engine keeps more)."""
    session.flush()
    theirs = text_lines(remote.screen)
    ours = text_lines(session.screen)
    return (
        len(theirs) <= len(ours)
        and theirs == ours[len(ours) - len(theirs) :]
        and (remote.rows, remote.cols) == (session.rows, session.cols)
    )


class Viewer:
    def __init__(self) -> None:
        self.chunks: list[tuple[int, bytes]] = []
        self.closed = False

    def __call__(self, offset: int, data: bytes) -> None:
        self.chunks.append((offset, data))

    def close(self) -> None:
        self.closed = True


async def _view(backend, key, agent_id):
    remote = await backend.session(key, agent_id)
    viewer = Viewer()
    detach = remote.attach(viewer, on_close=viewer.close)
    await until(lambda: remote.attached)
    return remote, viewer, detach


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


async def test_attach_renders_the_snapshot_then_the_live_output(engine, backend):
    info, session = await _spawn(engine)
    session.write("count 40\r")
    await _engine_shows(session, "done")
    await backend.sync()

    remote, viewer, _ = await _view(backend, info.worktree_path, info.agent_id)
    assert remote.attaches == ["snapshot"]
    await until(lambda: _same_screen(remote, session))

    remote.write("count 5\r")  # an input frame
    await _engine_shows(session, "line 5")
    await until(lambda: _same_screen(remote, session))
    assert (
        viewer.chunks
        and viewer.chunks[-1][0] + len(viewer.chunks[-1][1]) == remote.received
    )


async def test_a_gap_reattaches_from_the_last_byte_fed(engine, backend):
    info, session = await _spawn(engine)
    remote, _, _ = await _view(backend, info.worktree_path, info.agent_id)
    first = remote.stream_id

    # A frame that starts past what was fed: something went missing.
    remote._feed(remote.received + 7, b"garbage")
    await until(lambda: remote.attached and remote.stream_id != first)
    assert remote.gaps == 1
    assert remote.attaches == ["snapshot", "tail"]
    session.write("count 20\r")
    await _engine_shows(session, "done")
    await until(lambda: _same_screen(remote, session))


async def test_a_reset_stream_reattaches_from_its_offset(engine, backend):
    info, session = await _spawn(engine)
    remote, _, _ = await _view(backend, info.worktree_path, info.agent_id)
    [conn] = engine._v1_server._connections
    stream = conn.streams[remote.stream_id]
    # What the server does to a client that fell too far behind.
    conn.reset_stream(stream, session.ring.end_offset)
    session.write("count 10\r")
    await until(lambda: remote.attaches == ["snapshot", "tail"] and remote.attached)
    await _engine_shows(session, "done")
    await until(lambda: _same_screen(remote, session))


async def test_a_far_behind_reattach_gets_a_snapshot(engine, backend):
    info, session = await _spawn(engine)
    remote, _, detach = await _view(backend, info.worktree_path, info.agent_id)
    detach()
    await until(lambda: not remote.attached)
    session.write("spew 700000\r")  # more than the raw tail the engine sends
    await _engine_shows(session, "done")
    remote.attach(Viewer())
    await until(lambda: remote.attached)
    assert remote.attaches == ["snapshot", "snapshot"]
    await until(lambda: _same_screen(remote, session))


async def test_the_stream_follows_the_views(engine, backend):
    """Only shown terminals stream: no view, no stream (K2)."""
    info, session = await _spawn(engine)
    remote, _, detach = await _view(backend, info.worktree_path, info.agent_id)
    assert session.viewer_count == 1
    detach()
    await until(lambda: not remote.attached and session.viewer_count == 0)

    session.write("count 3\r")
    await _engine_shows(session, "done")
    remote.attach(Viewer())
    await until(lambda: remote.attached)
    assert remote.attaches == ["snapshot", "tail"]
    await until(lambda: _same_screen(remote, session))


async def test_the_screen_has_the_pty_size(engine, backend):
    info, session = await _spawn(engine)
    remote, _, _ = await _view(backend, info.worktree_path, info.agent_id)
    other = await Client.connect(engine.v1_socket_path)
    try:
        await other.request("claim_size", session_id=info.session_id, rows=30, cols=100)
        await until(lambda: (remote.screen.lines, remote.screen.columns) == (30, 100))
        assert (remote.rows, remote.cols) == (30, 100)

        # Asking for a size makes this client the owner, and the screen
        # follows once the PTY changed.
        remote.resize(40, 120)
        await until(lambda: (session.rows, session.cols) == (40, 120))
        await until(lambda: (remote.screen.lines, remote.screen.columns) == (40, 120))
        result = await other.call(
            "resize", {"session_id": info.session_id, "rows": 9, "cols": 9}
        )
        assert result.error is not None and result.error.code == "NOT_SIZE_OWNER"
    finally:
        await other.close()


async def test_an_exited_session_closes_its_views(engine, backend):
    info, _ = await _spawn(engine)
    remote, viewer, _ = await _view(backend, info.worktree_path, info.agent_id)
    remote.write("exit 3\r")
    await until(lambda: viewer.closed)
    assert not remote.running and remote.exit_code == 3
    with pytest.raises(RuntimeError):
        remote.write("x")


# ---------------------------------------------------------------------------
# The client store
# ---------------------------------------------------------------------------


async def test_the_store_follows_the_engine(engine, backend):
    info, session = await _spawn(engine)
    await backend.sync()
    feat = _feat(engine)
    assert [wt.path for wt in backend.worktrees] == [wt.path for wt in engine.worktrees]
    assert backend.agent_ids(feat) == [info.agent_id]
    assert backend.registry.label(feat, info.agent_id) == info.label
    assert backend.has_agent(feat)
    assert backend.agent_states(feat)[info.agent_id].status == session.status
    assert backend.has_active_agents(feat)
    assert backend.get_config() == engine.get_config()
    engine.world.statuses[feat] = GitStatus(
        dirty_count=2, has_upstream=True, last_commit_subject="the subject"
    )
    await engine.refresh_git_status()
    late = _remote(engine)
    await late.start()
    try:
        # Seeded with the full statuses, not only the counts.
        assert late.git_statuses == engine.git_statuses
        assert late.git_statuses[feat].last_commit_subject == "the subject"
    finally:
        await late.stop()

    engine.stop_agent(feat, info.agent_id)
    await backend.sync()
    assert not backend.has_agent(feat)
    assert backend.agent_states(feat) == {}


async def test_a_new_client_learns_the_running_agents(engine):
    info, _ = await _spawn(engine)
    backend = _remote(engine)
    spawned = []
    backend.subscribe(
        lambda e: spawned.append(e.data) if e.event == "agent.spawned" else None
    )
    await backend.start()
    try:
        assert [(d["worktree_path"], d["agent_id"]) for d in spawned] == [
            (info.worktree_path, info.agent_id)
        ]
        assert backend.agent_ids(info.worktree_path) == [info.agent_id]
    finally:
        await backend.stop()


async def test_a_new_client_learns_an_agent_with_no_status_yet(engine, monkeypatch):
    from tests.engine.fakes import no_pty

    no_pty(monkeypatch)
    info = await engine.spawn_agent(_feat(engine))
    backend = _remote(engine)
    await backend.start()
    try:
        assert backend.agent_ids(info.worktree_path) == [info.agent_id]
        assert backend.agent_states(info.worktree_path) == {}
        listed = await backend.request("list_agents", worktree_path=info.worktree_path)
        assert [a["status"] for a in listed["agents"]] == ["starting"]
        assert (await backend.session(info.worktree_path, info.agent_id)) is not None
    finally:
        await backend.stop()


async def test_requests_fail_like_the_engine_does(engine, backend):
    with pytest.raises(ValueError, match="Unknown worktree"):
        await backend.spawn_agent("/nowhere")
    with pytest.raises(ValueError, match="main worktree"):
        await backend.remove_worktree(engine.worktrees[0].path)


async def test_without_an_engine_the_backend_says_so_and_keeps_trying(tmp_path):
    backend = RemoteBackend(
        str(tmp_path), retry_initial=0.05, retry_max=0.1, first_connect_timeout=0.2
    )
    events = []
    backend.subscribe(events.append)
    await backend.start()
    try:
        assert not backend.connected
        [lost] = [e for e in events if e.event == "backend.disconnected"]
        assert "no engine serves" in lost.data["reason"]
        assert "lazyagent engine start" in lost.data["reason"]
        await asyncio.sleep(0.3)  # retried, still reported once
        assert sum(e.event == "backend.disconnected" for e in events) == 1
    finally:
        await backend.stop()


async def test_make_backend_reads_the_environment(tmp_path):
    assert isinstance(make_backend(str(tmp_path), {}), InProcessBackend)
    remote = make_backend(
        str(tmp_path),
        {"LAZYAGENT_ENGINE": "remote", "LAZYAGENT_ENGINE_SOCKET": "/x/engine.sock"},
    )
    assert isinstance(remote, RemoteBackend)
    assert remote._socket_option == "/x/engine.sock"
    with pytest.raises(ValueError, match="LAZYAGENT_ENGINE"):
        make_backend(str(tmp_path), {"LAZYAGENT_ENGINE": "cloud"})


# ---------------------------------------------------------------------------
# The TUI on a remote engine
# ---------------------------------------------------------------------------


def _app(engine) -> LazyAgent:
    return LazyAgent(repo_path=engine.repo_path, backend=_remote(engine))


async def _show(app, pilot, path) -> None:
    app._queue_selection(path)
    await until(lambda: app._shown == path)
    await pilot.pause()


async def _agent_terminal(app, pilot, engine, info):
    panel = app.query_one(CenterPanel).get_panel(info.worktree_path)

    async def mounted():
        await until(lambda: panel.get_agent(info.agent_id) is not None)

    await mounted()
    terminal = panel.get_agent(info.agent_id)
    await until(lambda: terminal.session.attached)
    return terminal


async def test_the_view_crops_a_wider_pty_and_typing_takes_the_size_back(engine):
    info, session = await _spawn(engine)
    app = _app(engine)
    async with app.run_test(size=(120, 40)) as pilot:
        await _show(app, pilot, info.worktree_path)
        terminal = await _agent_terminal(app, pilot, engine, info)
        await until(
            lambda: (session.rows, session.cols) == (terminal.nrow, terminal.ncol)
        )

        other = await Client.connect(engine.v1_socket_path)
        try:
            await other.request(
                "claim_size", session_id=info.session_id, rows=25, cols=200
            )
            remote = terminal.session
            await until(lambda: remote.screen.columns == 200)
            await pilot.pause()
            width = terminal.scrollable_content_region.width
            assert width < 200
            # The display is the PTY's size; the view crops it.
            assert terminal.render_line(0).cell_length == width
            assert (session.rows, session.cols) == (25, 200)

            terminal.focus()
            await pilot.press("a")
            await until(
                lambda: (session.rows, session.cols) == (terminal.nrow, terminal.ncol)
            )
            await until(lambda: remote.screen.columns == terminal.ncol)
        finally:
            await other.close()


async def test_a_hidden_terminal_streams_nothing_and_never_claims(engine):
    info, session = await _spawn(engine)
    app = _app(engine)
    async with app.run_test(size=SIZE) as pilot:
        await _show(app, pilot, info.worktree_path)
        terminal = await _agent_terminal(app, pilot, engine, info)
        remote = terminal.session

        await _show(app, pilot, engine.worktrees[0].path)
        await until(lambda: not remote.attached and session.viewer_count == 0)
        other = await Client.connect(engine.v1_socket_path)
        try:
            await other.request(
                "claim_size", session_id=info.session_id, rows=20, cols=70
            )
            await pilot.resize_terminal(140, 45)
            await pilot.pause()
            await asyncio.sleep(0.2)
            assert (session.rows, session.cols) == (20, 70)
            size = await other.request("claim_size", session_id=info.session_id)
            assert size["owner"] is True

            await _show(app, pilot, info.worktree_path)
            await until(lambda: remote.attached)
            assert remote.attaches[-1] == "tail"
        finally:
            await other.close()


async def test_losing_the_connection_warns_and_reconnects(engine):
    app = _app(engine)
    async with app.run_test(size=SIZE) as pilot:
        await _show(app, pilot, _feat(engine))
        shell = (
            app.query_one(CenterPanel)
            .get_panel(_feat(engine))
            .query_one("#terminal-widget")
        )
        await until(lambda: shell.session.attached)
        app.notify = MagicMock()

        for conn in list(engine._v1_server._connections):
            conn.close()
        await until(lambda: app.notify.call_count >= 2, timeout=10)
        messages = [c.args[0] for c in app.notify.call_args_list]
        assert "Lost the engine" in messages[0]
        assert "Reconnected" in messages[1]
        assert app.backend.connected

        # Same engine: the shell resumes where it was.
        await until(lambda: shell.session.attached)
        assert shell.session.attaches == ["snapshot", "tail"]
        shell.session.write("echo AFTER_\\RECONNECT\r")
        await until(lambda: "AFTER_RECONNECT" in shell.session.screen_text())


async def test_an_engine_that_comes_back_is_resynced(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = _engine(tmp_path)
    await first.start()
    info, _ = await _spawn(first)
    app = _app(first)
    second = None
    try:
        async with app.run_test(size=SIZE) as pilot:
            await _show(app, pilot, info.worktree_path)
            await _agent_terminal(app, pilot, first, info)
            panel = app.query_one(CenterPanel).get_panel(info.worktree_path)
            old_shell = panel.query_one("#terminal-widget").session
            app.notify = MagicMock()

            await first.stop()  # the agents die with it (K3)
            await until(lambda: not app.backend.connected)
            await until(lambda: app.notify.call_count >= 1)
            assert "Lost the engine" in app.notify.call_args_list[0].args[0]
            assert app.sub_title == "engine disconnected"

            second = _engine(tmp_path)
            await second.start()
            await until(lambda: app.backend.connected, timeout=10)
            await until(lambda: panel.agent_ids == [])
            await until(
                lambda: panel.query_one("#terminal-widget").session is not old_shell
            )
            shell = panel.query_one("#terminal-widget").session
            assert not old_shell.running
            await until(lambda: shell.attached)
            shell.write("echo NEW_\\ENGINE\r")
            await until(lambda: "NEW_ENGINE" in shell.screen_text())
    finally:
        await first.stop()
        if second is not None:
            await second.stop()


async def test_quitting_the_tui_leaves_the_agents_running(engine):
    feat = _feat(engine)
    app = _app(engine)
    async with app.run_test(size=SIZE) as pilot:
        await _show(app, pilot, feat)
        info = await app.backend.spawn_agent(feat)
        terminal = await _agent_terminal(app, pilot, engine, info)
        session = engine.get_session(feat, info.agent_id)
        await _engine_shows(session, "your turn>")
        terminal.focus()
        await pilot.press(*"count", "space", "1", "2")
        # A pilot "enter" carries no character; a terminal's Enter is CR.
        terminal.session.write("\r")
        await _engine_shows(session, "done")
        await pilot.press("ctrl+k")
        await pilot.press("q")
    assert not app.is_running
    # The engine and its agent are still there.
    assert engine.has_agent(feat)
    assert session.running
    session.write("count 3\r")
    await _engine_shows(session, "line 3")

    again = _app(engine)
    async with again.run_test(size=SIZE) as pilot:
        await _show(again, pilot, feat)
        terminal = await _agent_terminal(again, pilot, engine, info)
        remote = terminal.session
        assert remote.attaches == ["snapshot"]
        await until(lambda: _same_screen(remote, session))
        assert "line 12" in "\n".join(text_lines(remote.screen))
        await pilot.press("ctrl+k")
        await pilot.press("q")
    assert session.running


async def test_the_shell_takes_the_keyboard_once_its_view_is_there(engine):
    """Opening a worktree's shell is a round trip: Ctrl+L right after the
    first visit focuses the shell when its view is mounted."""
    app = _app(engine)
    async with app.run_test(size=SIZE) as pilot:
        feat = _feat(engine)
        app._queue_selection(feat)
        await until(lambda: app._shown == feat)
        panel = app.query_one(CenterPanel).get_panel(feat)
        app.action_focus_terminal()
        await until(lambda: panel.shell_ready.is_set())
        await pilot.pause()
        assert app.focused is panel.query_one("#terminal-widget")
