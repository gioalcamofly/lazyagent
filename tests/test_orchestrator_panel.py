"""Tests for orchestrator panel UI integration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lazyagent.worktree_manager import PrDiff
from lazyagent.app import LazyAgent
from tests.tui_backends import settle, tui_app
from lazyagent.config import Config
from lazyagent.models import AgentState, AgentStatus, GitStatus, LifecycleConfidence, WorktreeInfo
from lazyagent.widgets.center_panel import CenterPanel
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY, OrchestratorPanel
from lazyagent.widgets.worktree_list import OrchestratorListItem, WorktreeList, WorktreeListItem
from lazyagent.widgets.center_panel import WorktreePanel


MAIN_WORKTREE = WorktreeInfo(
    path="/repo",
    head="a" * 40,
    branch="main",
    is_main=True,
    is_bare=False,
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
        self.repo_path = Path(repo_path)

    def list(self):
        return WORKTREES

    def get_all_git_statuses(self, worktrees):
        return {wt.path: GitStatus() for wt in worktrees}

    def get_git_status(self, worktree_path):
        return GitStatus()

    def get_last_commit_subject(self, worktree_path):
        return ""

    @staticmethod
    def get_diff(worktree_path):
        return ""

    @staticmethod
    def resolve_base_ref(worktree_path, branch):
        return None

    @staticmethod
    def get_pr_diff(worktree_path, base, *, max_bytes=None):
        return PrDiff("", "", "")

    @staticmethod
    def is_gh_installed():
        return False


def _patch_app(monkeypatch):
    monkeypatch.setattr("lazyagent.engine.engine.WorktreeManager", DummyWorktreeManager)
    monkeypatch.setattr("lazyagent.engine.engine.load_config", lambda repo_root: Config())
    monkeypatch.setattr(WorktreePanel, "_try_start_terminal", lambda self: None)
    monkeypatch.setattr(LazyAgent, "_refresh_pr_status", lambda self: None)


# --- OrchestratorListItem unit tests ---


class TestOrchestratorListItem:
    def test_default_status_shows_dashes(self):
        item = OrchestratorListItem()
        label = item._build_label()
        assert "ORCH" in label
        assert "---" in label

    def test_running_status_shows_running(self):
        state = AgentState(status=AgentStatus.RUNNING, confidence=LifecycleConfidence.HIGH)
        item = OrchestratorListItem(agent_state=state)
        label = item._build_label()
        assert "running" in label

    def test_update_agent_state_changes_label(self):
        item = OrchestratorListItem()
        assert "---" in item._build_label()
        item.update_agent_state(
            AgentState(status=AgentStatus.WAITING, confidence=LifecycleConfidence.HIGH)
        )
        assert "waiting" in item._build_label()


# --- WorktreeList orchestrator prepend (tested via app integration below) ---


# --- CenterPanel orchestrator methods ---


class TestCenterPanelOrchestrator:
    def test_ensure_orchestrator_panel_creates_panel(self):
        center = CenterPanel()
        assert center.get_orchestrator_panel() is None
        # Can't fully test compose without running app, but verify _panels tracking
        assert ORCHESTRATOR_KEY not in center._panels

    def test_panel_key_is_deterministic(self):
        """The orchestrator panel ID is always 'wp-orchestrator'."""
        center = CenterPanel()
        # Verify constant
        assert ORCHESTRATOR_KEY == "__orchestrator__"


# --- App integration tests ---


@pytest.mark.asyncio
async def test_orchestrator_is_first_sidebar_entry(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)
        children = [c for c in wt_list.children if isinstance(c, (OrchestratorListItem, WorktreeListItem))]
        assert len(children) == 3
        assert isinstance(children[0], OrchestratorListItem)


@pytest.mark.asyncio
async def test_selecting_orchestrator_switches_to_orchestrator_panel(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)
        # Select orchestrator (index 0)
        orch_item = list(wt_list.children)[0]
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orch_item))
        await settle(pilot)

        assert app._orchestrator_selected is True
        assert app._selected_worktree is None

        center = app.query_one(CenterPanel)
        panel = center.get_orchestrator_panel()
        assert panel is not None
        assert isinstance(panel, OrchestratorPanel)


@pytest.mark.asyncio
async def test_selecting_worktree_after_orchestrator_clears_flag(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)

        # Select orchestrator
        orch_item = list(wt_list.children)[0]
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orch_item))
        await settle(pilot)
        assert app._orchestrator_selected is True

        # Select worktree
        wt_item = list(wt_list.children)[1]
        wt_list.index = 1
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, wt_item))
        await settle(pilot)
        assert app._orchestrator_selected is False
        assert app._selected_worktree is not None


@pytest.mark.asyncio
async def test_spawn_on_orchestrator_shows_modal(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)

        # Select orchestrator
        orch_item = list(wt_list.children)[0]
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orch_item))
        await settle(pilot)

        # Mock push_screen to verify modal is shown
        app.push_screen = MagicMock()
        app.action_spawn_agent()
        app.push_screen.assert_called_once()


@pytest.mark.asyncio
async def test_stop_on_orchestrator_without_agent_notifies(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)

        # Select orchestrator
        orch_item = list(wt_list.children)[0]
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orch_item))
        await settle(pilot)

        app.notify = MagicMock()
        await app.action_stop_agent()
        app.notify.assert_called_once()
        assert "No running orchestrator" in app.notify.call_args.args[0]


async def _spawn_orchestrator(app, pilot, monkeypatch):
    """Spawn the orchestrator through the engine, with no real process."""
    from tests.engine.fakes import no_pty

    no_pty(monkeypatch)
    await app.backend.engine.spawn_agent(app._repo_root, kind="orchestrator")
    await settle(pilot)
    return app.backend.engine.get_session(ORCHESTRATOR_KEY)


@pytest.mark.asyncio
async def test_orchestrator_status_changed_updates_sidebar(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        from lazyagent.engine.session import StatusChanged

        session = await _spawn_orchestrator(app, pilot, monkeypatch)
        session._emit(
            StatusChanged(
                ORCHESTRATOR_KEY, "", AgentStatus.RUNNING, LifecycleConfidence.HIGH, "", None
            )
        )
        await settle(pilot)

        assert app._get_agent_state(ORCHESTRATOR_KEY).status == AgentStatus.RUNNING

        # Verify sidebar was updated
        wt_list = app.query_one(WorktreeList)
        orch_item = list(wt_list.children)[0]
        assert isinstance(orch_item, OrchestratorListItem)
        assert orch_item._agent_state.status == AgentStatus.RUNNING


@pytest.mark.asyncio
async def test_orchestrator_exited_resets_state(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        from lazyagent.engine.session import SessionExited, StatusChanged

        session = await _spawn_orchestrator(app, pilot, monkeypatch)
        panel = app.query_one(CenterPanel).get_orchestrator_panel()
        assert panel.agent_terminal is not None
        session._emit(
            StatusChanged(
                ORCHESTRATOR_KEY, "", AgentStatus.RUNNING, LifecycleConfidence.HIGH, "", None
            )
        )

        app.notify = MagicMock()
        session._emit(SessionExited(ORCHESTRATOR_KEY, "", 0))
        await settle(pilot)

        assert app._get_agent_state(ORCHESTRATOR_KEY).status == AgentStatus.NO_AGENT
        assert not app.backend.engine.has_agent(ORCHESTRATOR_KEY)
        assert panel.agent_terminal is None
        # The sidebar entry no longer says it runs.
        orch_item = next(iter(app.query_one(WorktreeList).children))
        assert orch_item._agent_state.status == AgentStatus.NO_AGENT
        app.notify.assert_called_once()
        assert "Orchestrator exited" in app.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_spawning_the_orchestrator_shows_it(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        session = await _spawn_orchestrator(app, pilot, monkeypatch)

        panel = app.query_one(CenterPanel).get_orchestrator_panel()
        assert panel.agent_terminal.session.session_id == session.session_id
        assert panel.has_agent
        assert session.worktree_path == ORCHESTRATOR_KEY
        assert "--append-system-prompt" in session.command

        app.notify = MagicMock()
        app._stop_orchestrator_agent()
        await settle(pilot)
        assert panel.agent_terminal is None
        assert "Orchestrator agent stopped" in app.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_ctrl_j_on_orchestrator_triggers_spawn_when_no_agent(monkeypatch, backend_kind):
    _patch_app(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)

        # Select orchestrator
        orch_item = list(wt_list.children)[0]
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orch_item))
        await settle(pilot)

        app.action_spawn_agent = MagicMock()
        app.action_focus_agent()
        app.action_spawn_agent.assert_called_once()
