"""A host driving the real TUI (LAZYAGENT_HOST set) over the v1 socket: its
ui.* commands change what the app shows, and ui.view_changed reports it.

The app runs under ``run_test`` with its own in-process engine (and so its
own v1 server), on the fake git world of tests/hosted_app.py.
"""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.app import LazyAgent
from lazyagent.engine.client import Client, RequestError
from lazyagent.protocol import UI_COMMAND_METHODS, validation
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.prompt_modal import SpawnModal
from tests.contract.helpers import checked, spawn, until
from tests.hosted_app import make_app

pytestmark = pytest.mark.asyncio

# Big enough that an agent tab never makes the PTY shrink: pyte drops the top
# rows on a shrink, and the fake agent does not redraw on SIGWINCH.
SIZE = (160, 90)


async def _connect(app) -> Client:
    assert app.backend.engine.v1_socket_path is not None
    # The app announces its first view once it settles: before the host
    # subscribes, so that event is not mistaken for a command's.
    await until(lambda: app.backend.engine.ui_view is not None)
    client = await Client.connect(app.backend.engine.v1_socket_path, client="host")
    await checked(client, "subscribe", events=["ui.view_changed"])
    return client


async def _view(client, **expected) -> dict:
    """The next ui.view_changed whose fields match ``expected``."""

    def matches(event) -> bool:
        return event.event == "ui.view_changed" and all(
            event.data.get(k) == v for k, v in expected.items()
        )

    seen = []

    def logged(event) -> bool:
        seen.append(event.data)
        return matches(event)

    try:
        event = await client.next_event(logged, timeout=10)
    except TimeoutError:
        raise AssertionError(f"no view with {expected} in {seen}") from None
    validation.validate_event("ui.view_changed", event.data, strict=True)
    return event.data


async def test_host_selects_a_worktree_and_opens_its_shell(
    tmp_path, monkeypatch, backend_kind
):
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE) as pilot:
        client = await _connect(app)
        try:
            assert set(UI_COMMAND_METHODS) <= set(client.hello.capabilities)
            await checked(client, "ui.select_worktree", worktree_path=app.feat)
            # No agent yet: the keyboard goes to the shell.
            await _view(client, worktree_path=app.feat, focus="shell")
            assert app._selected_worktree.path == app.feat

            app.action_focus_diff()
            await _view(client, worktree_path=app.feat, focus="diff")

            await checked(client, "ui.open_shell")
            await _view(client, worktree_path=app.feat, focus="shell")
            assert app.focused is app.query_one("#terminal-widget")

            await checked(client, "ui.show_diff")
            await _view(client, worktree_path=app.feat, focus="diff")

            await checked(client, "ui.select_orchestrator")
            await _view(client, orchestrator=True, worktree_path=None)
            assert app._orchestrator_selected
            assert await checked(client, "ui.get_view") == {
                "view": app.backend.engine.ui_view
            }
            await pilot.pause()
        finally:
            await client.close()


async def test_host_focuses_an_agent(tmp_path, monkeypatch, backend_kind):
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE):
        client = await _connect(app)
        try:
            await checked(client, "ui.select_worktree", worktree_path=app.feat)
            await _view(client, worktree_path=app.feat)
            first = await spawn(client, app.feat)
            second = await spawn(client, app.feat)
            # The second tab is up and focused: a later mount would take the
            # focus back.
            await _view(client, agent_id=second["agent_id"], focus="agent")
            await until(
                lambda: (
                    isinstance(app.focused, MonitoredTerminal)
                    and app.focused.session.agent_id == second["agent_id"]
                )
            )
            await checked(
                client,
                "ui.focus_agent",
                worktree_path=app.feat,
                agent_id=first["agent_id"],
            )
            view = await _view(client, agent_id=first["agent_id"], focus="agent")
            assert view["worktree_path"] == app.feat
            focused = app.focused
            assert isinstance(focused, MonitoredTerminal)
            assert focused.session.agent_id == first["agent_id"]

            await checked(client, "ui.focus_agent", agent_id=second["agent_id"])
            await _view(client, agent_id=second["agent_id"], focus="agent")
        finally:
            await client.close()


async def test_host_opens_the_spawn_modal(tmp_path, monkeypatch, backend_kind):
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE) as pilot:
        client = await _connect(app)
        try:
            await checked(
                client, "ui.open_modal", modal="spawn", worktree_path=app.feat
            )
            await _view(client, focus="modal", modal="spawn", worktree_path=app.feat)
            assert isinstance(app.screen, SpawnModal)
            with pytest.raises(RequestError) as e:
                await client.request("ui.open_modal", modal="create")
            assert "already open" in e.value.message
            await pilot.press("escape")
            await until(lambda: not isinstance(app.screen, SpawnModal))
            view = await _view(client, worktree_path=app.feat)
            assert view["focus"] != "modal"
        finally:
            await client.close()


async def test_selecting_an_unknown_worktree_changes_nothing(
    tmp_path, monkeypatch, backend_kind
):
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE):
        client = await _connect(app)
        try:
            await checked(client, "ui.select_worktree", worktree_path=app.feat)
            await _view(client, worktree_path=app.feat)
            with pytest.raises(RequestError) as e:
                await client.request("ui.select_worktree", worktree_path="/nope")
            assert e.value.code == "VALIDATION_ERROR"
            assert app._selected_worktree.path == app.feat
        finally:
            await client.close()


async def test_a_plain_tui_takes_no_commands_but_reports_its_view(
    tmp_path, monkeypatch, backend_kind
):
    app = make_app(tmp_path, monkeypatch, hosted=False, backend_kind=backend_kind)
    async with app.run_test(size=SIZE):
        client = await _connect(app)
        try:
            assert not set(UI_COMMAND_METHODS) & set(client.hello.capabilities)
            with pytest.raises(RequestError) as e:
                await client.request("ui.select_worktree", worktree_path=app.feat)
            assert e.value.code == "NO_HOSTED_UI"
            app._queue_selection(app.feat)
            await _view(client, worktree_path=app.feat)
        finally:
            await client.close()


async def test_one_host_action_is_one_view_change(tmp_path, monkeypatch, backend_kind):
    """A selection mounts the panel and then focuses the shell: the host
    hears only where it ends."""
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE):
        client = await _connect(app)
        try:
            for path in (app.feat, app.repo):
                await checked(client, "ui.select_worktree", worktree_path=path)
                event = await client.next_event(
                    lambda e: e.event == "ui.view_changed", timeout=10
                )
                assert (event.data["worktree_path"], event.data["focus"]) == (
                    path,
                    "shell",
                )
                await asyncio.sleep(0.3)
                assert [
                    e for e in client.pending_events() if e.event == "ui.view_changed"
                ] == []
        finally:
            await client.close()


async def test_a_busy_app_announces_where_the_focus_lands(
    tmp_path, monkeypatch, backend_kind
):
    """The focus a command asks for is queued on the app: a handler still
    awaiting ahead of it must not let the view go out without it."""

    async def busy() -> None:
        await asyncio.sleep(0.2)

    focus_shown = LazyAgent._focus_shown

    def busy_focus_shown(self) -> None:
        self.call_later(busy)
        focus_shown(self)

    monkeypatch.setattr(LazyAgent, "_focus_shown", busy_focus_shown)
    app = make_app(tmp_path, monkeypatch, hosted=True, backend_kind=backend_kind)
    async with app.run_test(size=SIZE):
        client = await _connect(app)
        try:
            await checked(client, "ui.select_worktree", worktree_path=app.feat)
            event = await client.next_event(
                lambda e: e.event == "ui.view_changed", timeout=10
            )
            assert (event.data["worktree_path"], event.data["focus"]) == (
                app.feat,
                "shell",
            )
        finally:
            await client.close()
