"""Tests for multiple agents per worktree (WorktreePanel + sidebar roll-up)."""
from __future__ import annotations

import pytest
from textual.widgets import TabbedContent, TabPane

from lazyagent.app import LazyAgent
from lazyagent.config import Config
from lazyagent.models import AgentState, AgentStatus
from lazyagent.widgets.center_panel import (
    CenterPanel,
    WorktreePanel,
    _DIFF_TAB_ID,
    _PLACEHOLDER_TAB_ID,
)
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.worktree_list import format_agents_status
from lazyagent.models import WorktreeInfo, GitStatus
from lazyagent.widgets.worktree_list import WorktreeList


MAIN_WORKTREE = WorktreeInfo(
    path="/repo", head="a" * 40, branch="main", is_main=True, is_bare=False
)
FEATURE_WORKTREE = WorktreeInfo(
    path="/repo-feature",
    head="b" * 40,
    branch="feature/demo",
    is_main=False,
    is_bare=False,
)
WORKTREES = [MAIN_WORKTREE, FEATURE_WORKTREE]


class DummyWorktreeManager:
    def __init__(self, repo_path):
        from pathlib import Path

        self.repo_path = Path(repo_path)

    def list(self):
        return WORKTREES

    def get_all_git_statuses(self, worktrees):
        return {wt.path: GitStatus() for wt in worktrees}

    @staticmethod
    def get_diff(worktree_path):
        return ""

    @staticmethod
    def is_gh_available():
        return False


@pytest.fixture
def patched(monkeypatch):
    """Patch out app I/O and the agent pty so spawn_agent only drives the UI."""
    monkeypatch.setattr("lazyagent.app.WorktreeManager", DummyWorktreeManager)
    monkeypatch.setattr("lazyagent.app.load_config", lambda repo_root: Config())
    monkeypatch.setattr(WorktreePanel, "_try_start_terminal", lambda self: None)
    monkeypatch.setattr(LazyAgent, "_refresh_pr_status", lambda self: None)

    def fake_start(self):
        # Pretend the pty is live without spawning a subprocess. Real queues
        # so on_resize/on_show (which put set_size messages) don't blow up.
        import asyncio

        self.emulator = object()
        self.send_queue = asyncio.Queue()
        self.recv_queue = asyncio.Queue()

    monkeypatch.setattr(MonitoredTerminal, "start", fake_start)
    monkeypatch.setattr(MonitoredTerminal, "stop", lambda self: None)
    monkeypatch.setattr(MonitoredTerminal, "focus", lambda self: self)


def _agent_panes(panel: WorktreePanel) -> list[str]:
    tabs = panel.query_one("#agent-tabs", TabbedContent)
    return [
        pane.id
        for pane in tabs.query(TabPane)
        if pane.id and pane.id.startswith("agent-tab-")
    ]


@pytest.mark.asyncio
async def test_spawn_adds_a_tab_per_agent(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        center = app.query_one(CenterPanel)
        panel = await center.switch_to(FEATURE_WORKTREE.path)
        await pilot.pause()

        a1 = await panel.spawn_agent()
        a2 = await panel.spawn_agent()
        await pilot.pause()

        assert [a1, a2] == ["a1", "a2"]
        assert panel.agent_ids == ["a1", "a2"]
        assert _agent_panes(panel) == ["agent-tab-a1", "agent-tab-a2"]

        # Newest agent's tab is active, and the placeholder tab is gone.
        tabs = panel.query_one("#agent-tabs", TabbedContent)
        assert tabs.active == "agent-tab-a2"
        with pytest.raises(Exception):
            panel.query_one(f"#{_PLACEHOLDER_TAB_ID}", TabPane)

        # Diff tab is still present and rightmost.
        assert panel.query_one(f"#{_DIFF_TAB_ID}", TabPane) is not None


@pytest.mark.asyncio
async def test_cleanup_one_agent_keeps_the_others(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        center = app.query_one(CenterPanel)
        panel = await center.switch_to(FEATURE_WORKTREE.path)
        await panel.spawn_agent()
        await panel.spawn_agent()
        await pilot.pause()

        await panel.cleanup_agent("a1")
        await pilot.pause()

        assert panel.agent_ids == ["a2"]
        assert _agent_panes(panel) == ["agent-tab-a2"]
        # Placeholder not restored while an agent remains.
        with pytest.raises(Exception):
            panel.query_one(f"#{_PLACEHOLDER_TAB_ID}", TabPane)


@pytest.mark.asyncio
async def test_cleanup_last_agent_restores_placeholder(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        center = app.query_one(CenterPanel)
        panel = await center.switch_to(FEATURE_WORKTREE.path)
        await panel.spawn_agent()
        await pilot.pause()

        await panel.cleanup_agent("a1")
        await pilot.pause()

        assert panel.agent_ids == []
        assert _agent_panes(panel) == []
        # Placeholder tab is back.
        assert panel.query_one(f"#{_PLACEHOLDER_TAB_ID}", TabPane) is not None


@pytest.mark.asyncio
async def test_active_agent_id_falls_back_to_sole_agent(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        center = app.query_one(CenterPanel)
        panel = await center.switch_to(FEATURE_WORKTREE.path)
        await panel.spawn_agent()
        await pilot.pause()

        panel.switch_to_tab(_DIFF_TAB_ID)
        await pilot.pause()
        # Even with Diff active, the sole agent is the implicit target.
        assert panel.active_agent_id == "a1"


@pytest.mark.asyncio
async def test_active_agent_id_none_when_diff_active_and_multiple(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        center = app.query_one(CenterPanel)
        panel = await center.switch_to(FEATURE_WORKTREE.path)
        await panel.spawn_agent()
        await panel.spawn_agent()
        await pilot.pause()

        panel.switch_to_tab(_DIFF_TAB_ID)
        await pilot.pause()
        # Ambiguous: Diff active and more than one agent -> no implicit target.
        assert panel.active_agent_id is None


@pytest.mark.asyncio
async def test_spawn_then_stop_via_app_action(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)
        from lazyagent.widgets.worktree_list import WorktreeListItem

        item = [c for c in wt_list.children if isinstance(c, WorktreeListItem)][1]
        wt_list.index = 2  # orchestrator(0) + main(1) + feature(2)
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, item))
        await pilot.pause()

        center = app.query_one(CenterPanel)
        panel = center.get_panel(FEATURE_WORKTREE.path)
        await panel.spawn_agent()
        await panel.spawn_agent()
        await pilot.pause()

        # Two agents tracked? The panel knows; the registry is populated as
        # status events arrive, so drive a stop of the active (newest) agent.
        app.notify = lambda *a, **k: None
        await app.action_stop_agent()
        await pilot.pause()

        assert panel.agent_ids == ["a1"]


async def _select_feature(app, pilot):
    """Highlight the feature worktree and return its panel."""
    from lazyagent.widgets.worktree_list import WorktreeListItem

    wt_list = app.query_one(WorktreeList)
    item = [c for c in wt_list.children if isinstance(c, WorktreeListItem)][1]
    wt_list.index = 2  # orchestrator(0) + main(1) + feature(2)
    await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, item))
    await pilot.pause()
    return app.query_one(CenterPanel).get_panel(FEATURE_WORKTREE.path)


@pytest.mark.asyncio
async def test_cycle_agents_wraps_both_directions(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        panel = await _select_feature(app, pilot)
        await panel.spawn_agent()  # a1
        await panel.spawn_agent()  # a2
        await panel.spawn_agent()  # a3 (active)
        await pilot.pause()

        assert panel.active_agent_id == "a3"

        app.action_next_agent()  # wraps a3 -> a1
        await pilot.pause()
        assert panel.active_agent_id == "a1"

        app.action_next_agent()  # a1 -> a2
        await pilot.pause()
        assert panel.active_agent_id == "a2"

        app.action_prev_agent()  # a2 -> a1
        await pilot.pause()
        assert panel.active_agent_id == "a1"

        app.action_prev_agent()  # wraps a1 -> a3
        await pilot.pause()
        assert panel.active_agent_id == "a3"


@pytest.mark.asyncio
async def test_cycle_agents_noop_with_single_agent(patched):
    app = LazyAgent(repo_path="/repo")
    async with app.run_test() as pilot:
        panel = await _select_feature(app, pilot)
        await panel.spawn_agent()  # a1
        await pilot.pause()

        app.action_next_agent()
        app.action_prev_agent()
        await pilot.pause()
        assert panel.active_agent_id == "a1"


# --- Pure roll-up formatting ---


def test_rollup_no_agents():
    assert "---" in format_agents_status({})


def test_rollup_single_agent_matches_single_format():
    states = {"a1": AgentState(status=AgentStatus.RUNNING)}
    out = format_agents_status(states)
    assert "running" in out
    assert "+1" not in out


def test_rollup_multiple_shows_dominant_and_overflow():
    states = {
        "a1": AgentState(status=AgentStatus.RUNNING),
        "a2": AgentState(status=AgentStatus.WAITING_FOR_USER),
    }
    out = format_agents_status(states)
    # WAITING_FOR_USER outranks RUNNING in the roll-up.
    assert "waiting" in out
    assert "(+1)" in out
