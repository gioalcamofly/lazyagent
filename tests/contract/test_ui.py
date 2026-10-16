"""The hosted-UI channel at the protocol level: ui.* commands become
ui.command events, ui.view_changed is what a UI announces, and
get_sidebar_state bundles what a host's sidebar needs.

A stand-in UI is attached through the harness here; tests/test_hosted.py
drives the real TUI.
"""

from __future__ import annotations

import pytest

from lazyagent.engine.client import RequestError
from lazyagent.protocol import UI_COMMAND_METHODS, validation
from tests.contract.harness import NotSupported
from tests.contract.helpers import checked, spawn

pytestmark = pytest.mark.asyncio


def _view(**fields):
    view = {
        "worktree_path": None,
        "orchestrator": False,
        "agent_id": None,
        "focus": "none",
        "modal": None,
    }
    view.update(fields)
    return view


@pytest.fixture
def ui(harness):
    try:
        detach = harness.attach_ui()
    except NotSupported as e:
        pytest.skip(str(e))
    yield harness
    detach()


async def _error(client, method, **params) -> RequestError:
    with pytest.raises(RequestError) as e:
        await client.request(method, **params)
    return e.value


async def test_without_a_hosted_ui_the_commands_fail_clearly(harness, client):
    for method in UI_COMMAND_METHODS:
        error = await _error(client, method, worktree_path=harness.feat)
        assert error.code == "NO_HOSTED_UI", method
        assert "LAZYAGENT_HOST" in error.message
    assert await checked(client, "ui.get_view") == {"view": None}


async def test_a_hosted_ui_is_advertised(ui):
    client = await ui.connect()
    assert set(UI_COMMAND_METHODS) <= set(client.hello.capabilities)


async def test_select_worktree_becomes_a_ui_command_event(ui):
    client = await ui.connect()
    await checked(client, "subscribe", events=["ui.command"])
    result = await checked(client, "ui.select_worktree", worktree_path=ui.feat)
    assert result["command"] == "select_worktree"
    assert result["worktree_path"] == ui.feat
    event = await client.next_event(lambda e: e.event == "ui.command")
    validation.validate_event("ui.command", event.data, strict=True)
    assert event.seq == result["seq"]
    assert event.data == {"command": "select_worktree", "worktree_path": ui.feat}


async def test_select_orchestrator(ui):
    client = await ui.connect()
    result = await checked(client, "ui.select_orchestrator")
    assert result["worktree_path"] is None


async def test_selecting_an_unknown_worktree_is_a_validation_error(ui):
    client = await ui.connect()
    error = await _error(client, "ui.select_worktree", worktree_path="/nope")
    assert error.code == "VALIDATION_ERROR"


async def test_targets_default_to_the_shown_worktree(ui):
    client = await ui.connect()
    error = await _error(client, "ui.open_shell")
    assert error.code == "VALIDATION_ERROR"  # nothing shown yet
    ui.publish_view(_view(worktree_path=ui.feat, focus="agent"))
    result = await checked(client, "ui.open_shell")
    assert result["worktree_path"] == ui.feat
    result = await checked(client, "ui.show_diff", worktree_path=ui.repo)
    assert result["worktree_path"] == ui.repo


async def test_the_orchestrator_has_no_shell(ui):
    client = await ui.connect()
    ui.publish_view(_view(orchestrator=True))
    assert (await _error(client, "ui.open_shell")).code == "VALIDATION_ERROR"
    assert (await _error(client, "ui.show_diff")).code == "VALIDATION_ERROR"


async def test_focus_agent_needs_a_running_agent(ui):
    client = await ui.connect()
    error = await _error(client, "ui.focus_agent", worktree_path=ui.feat)
    assert error.code == "VALIDATION_ERROR"
    agent = await spawn(client, ui.feat)
    result = await checked(
        client, "ui.focus_agent", worktree_path=ui.feat, agent_id=agent["agent_id"]
    )
    assert result["agent_id"] == agent["agent_id"]
    result = await checked(client, "ui.focus_agent", worktree_path=ui.feat)
    assert result["agent_id"] is None  # the UI picks the active tab
    error = await _error(client, "ui.focus_agent", worktree_path=ui.feat, agent_id="99")
    assert error.code == "VALIDATION_ERROR"
    error = await _error(client, "ui.focus_agent", orchestrator=True)
    assert error.code == "VALIDATION_ERROR"


async def test_open_modal(ui):
    client = await ui.connect()
    result = await checked(
        client, "ui.open_modal", modal="spawn", worktree_path=ui.feat
    )
    assert (result["modal"], result["worktree_path"]) == ("spawn", ui.feat)
    result = await checked(client, "ui.open_modal", modal="create")
    assert result["worktree_path"] is None
    result = await checked(client, "ui.open_modal", modal="spawn", orchestrator=True)
    assert result["worktree_path"] is None
    error = await _error(client, "ui.open_modal", modal="remove", worktree_path=ui.repo)
    assert "main worktree" in error.message
    error = await _error(client, "ui.open_modal", modal="nope", worktree_path=ui.feat)
    assert error.code == "VALIDATION_ERROR"
    ui.publish_view(_view(worktree_path=ui.feat, focus="modal", modal="spawn"))
    error = await _error(client, "ui.open_modal", modal="remove", worktree_path=ui.feat)
    assert "already open" in error.message


async def test_view_changed_and_get_view(ui):
    client = await ui.connect()
    await checked(client, "subscribe", events=["ui.view_changed"])
    view = _view(worktree_path=ui.feat, focus="shell")
    ui.publish_view(view)
    ui.publish_view(view)  # unchanged: not announced again
    ui.publish_view(_view(orchestrator=True))
    first = await client.next_event(lambda e: e.event == "ui.view_changed")
    second = await client.next_event(lambda e: e.event == "ui.view_changed")
    validation.validate_event("ui.view_changed", first.data, strict=True)
    assert first.data == view
    assert second.data["orchestrator"] is True
    assert await checked(client, "ui.get_view") == {"view": second.data}


async def test_sidebar_state_in_one_call(harness, client):
    agent = await spawn(client, harness.feat)
    state = await checked(client, "get_sidebar_state")
    assert [wt["path"] for wt in state["worktrees"]] == [harness.repo, harness.feat]
    feat = next(wt for wt in state["worktrees"] if wt["path"] == harness.feat)
    assert feat["agents"][0]["session_id"] == agent["session_id"]
    assert state["orchestrator"] == {"agents": []}
    assert state["pr_info"] == {}
    assert state["view"] is None
    await checked(client, "subscribe", since=state["cursor"])
    await checked(client, "refresh_worktrees")
    event = await client.next_event()
    assert event.seq > state["cursor"]
