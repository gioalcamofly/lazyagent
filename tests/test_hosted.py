"""Hosted mode (LAZYAGENT_HOST) in the Textual app: the compact layout, the
OSC announcement and the view the app publishes. tests/contract/
test_hosted_tui.py drives the same app over the v1 socket."""

from __future__ import annotations

import os

import pytest
from textual.drivers.headless_driver import HeadlessDriver
from textual.widgets import Footer, Header

from lazyagent.protocol.hosted import encode_osc, parse_osc, tmux_passthrough
from lazyagent.widgets.center_panel import CenterPanel, GitInfoBar
from lazyagent.widgets.worktree_list import WorktreeList
from tests.contract.helpers import until
from tests.hosted_app import make_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def written(monkeypatch):
    """Everything the app writes to its (headless) terminal."""
    chunks: list[str] = []
    monkeypatch.setattr(HeadlessDriver, "write", lambda self, data: chunks.append(data))
    return chunks


async def _show(app, pilot, path) -> None:
    app._queue_selection(path)
    await until(lambda: app._shown == path)
    await pilot.pause()


async def test_hosted_layout_keeps_only_the_terminals(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, hosted=True)
    async with app.run_test(size=(120, 40)) as pilot:
        await _show(app, pilot, app.feat)
        assert app.has_class("-hosted")
        assert not app.query_one("#sidebar").display
        assert app.query_one(WorktreeList).region.width == 0
        assert app.query_one("#pr-status-bar").region.width == 0
        assert not app.query_one(Header).display
        assert not app.query_one(Footer).display
        assert not app.query_one(GitInfoBar).display
        center = app.query_one(CenterPanel)
        assert center.region.width == 120
        assert center.region.height == 40
        terminal = app.query_one("#terminal-widget")
        assert terminal.display and terminal.region.width > 0
        assert app.query_one("#agent-tabs").region.width > 0


async def test_plain_layout_is_unchanged(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, hosted=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await _show(app, pilot, app.feat)
        assert not app.has_class("-hosted")
        assert app.query_one("#sidebar").region.width == 38
        assert app.query_one(Header).display
        assert app.query_one(Footer).display
        assert app.query_one(GitInfoBar).display
        assert app.query_one(CenterPanel).region.width == 120 - 38
        assert not app.backend.engine.ui_attached


async def test_ctrl_k_leaves_the_terminal_instead_of_focusing_the_sidebar(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path, monkeypatch, hosted=True)
    async with app.run_test() as pilot:
        await _show(app, pilot, app.feat)
        app.query_one("#terminal-widget").focus()
        await pilot.press("ctrl+k")
        assert app.focused is None
        assert not app.query_one(WorktreeList).can_focus
        await _view_is(app, focus="none")
        assert app.backend.engine.ui_attached
        await pilot.press("q")
        await until(
            lambda: not app.backend.engine.ui_attached
        )  # q quit: engine stopped


def _announcement(app) -> str:
    return encode_osc(
        {
            "socket": app.backend.engine.v1_socket_path,
            "engine_id": app.backend.engine.engine_id,
            "repo_root": app.repo,
            "pid": os.getpid(),
            "host": "test-host",
        }
    )


async def test_hosted_app_announces_its_socket(tmp_path, monkeypatch, written):
    app = make_app(tmp_path, monkeypatch, hosted=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        output = "".join(written)
        assert _announcement(app) in output
        [payload, *_] = parse_osc(output.encode())
        assert payload["v"] == 1
        assert payload["socket"] == app.backend.engine.v1_socket_path
        assert payload["engine_id"] == app.backend.engine.engine_id
        assert "\x1bPtmux;" not in output
        # Again after a resize, for a host that attached late.
        count = output.count(_announcement(app))
        await pilot.resize_terminal(100, 30)
        await pilot.pause()
        assert "".join(written).count(_announcement(app)) > count


async def test_inside_tmux_the_announcement_is_also_passed_through(
    tmp_path, monkeypatch, written
):
    app = make_app(tmp_path, monkeypatch, hosted=True)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    async with app.run_test() as pilot:
        await pilot.pause()
        expected = _announcement(app)
        assert expected + tmux_passthrough(expected) in "".join(written)


async def test_a_plain_app_writes_no_announcement(tmp_path, monkeypatch, written):
    app = make_app(tmp_path, monkeypatch, hosted=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "\x1b]7717;" not in "".join(written)


async def _view_is(app, **fields) -> None:
    """Wait for the published view (it settles after a short delay)."""

    def matches() -> bool:
        view = app.backend.engine.ui_view or {}
        return all(view.get(k) == v for k, v in fields.items())

    await until(matches)


async def test_the_view_follows_selection_and_focus(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, hosted=False)
    async with app.run_test() as pilot:
        await _show(app, pilot, app.feat)
        app.query_one("#terminal-widget").focus()
        await _view_is(app, focus="shell")
        assert app.backend.engine.ui_view == {
            "worktree_path": app.feat,
            "orchestrator": False,
            "agent_id": None,
            "focus": "shell",
            "modal": None,
        }
        app.action_focus_diff()
        await _view_is(app, focus="diff")
        await _show(app, pilot, None)
        await _view_is(app, orchestrator=True, worktree_path=None)
        app.action_help()
        await _view_is(app, focus="modal", modal="help")
        await pilot.press("escape")
        await until(lambda: app.backend.engine.ui_view["focus"] != "modal")


@pytest.mark.parametrize("hosted", [True, False])
async def test_help_describes_ctrl_k_for_the_mode(tmp_path, monkeypatch, hosted):
    app = make_app(tmp_path, monkeypatch, hosted=hosted)
    async with app.run_test() as pilot:
        app.action_help()
        await pilot.pause()
        text = str(app.screen.query_one("#help-text").render())
        assert ("Leave terminal" in text) is hosted
        assert ("Focus sidebar" in text) is not hosted
        assert ("j / k" in text) is not hosted
