from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.widgets import TabbedContent

from lazyagent.app import LazyAgent
from lazyagent.config import Config, WorktreeConfig
from lazyagent.models import AgentState, AgentStatus, GitStatus, WorktreeInfo
from lazyagent.widgets.center_panel import CenterPanel, WorktreePanel
from lazyagent.widgets.create_worktree_modal import CreateWorktreeResult
from lazyagent.widgets.remove_worktree_modal import RemoveWorktreeResult
from lazyagent.widgets.worktree_list import WorktreeList, WorktreeListItem


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


def _make_app_with_config(
    create: str | None = None,
    remove: str | None = None,
) -> LazyAgent:
    """Create a LazyAgent with custom worktree config and mocked I/O."""
    app = LazyAgent(repo_path="/repo")
    app._repo_root = "/repo"
    app._config = Config(worktree=WorktreeConfig(create=create, remove=remove))
    app._send_to_terminal = MagicMock()
    app.action_focus_terminal = MagicMock()
    app.notify = MagicMock()
    return app


class DummyWorktreeManager:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path)

    def list(self) -> list[WorktreeInfo]:
        return WORKTREES

    def get_all_git_statuses(
        self, worktrees: list[WorktreeInfo]
    ) -> dict[str, GitStatus]:
        return {
            wt.path: GitStatus(last_commit_subject=f"commit for {wt.name}")
            for wt in worktrees
        }

    @staticmethod
    def get_diff(worktree_path: str) -> str:
        return f"diff for {worktree_path}"

    @staticmethod
    def is_gh_available() -> bool:
        return False


def _patch_app_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("lazyagent.app.WorktreeManager", DummyWorktreeManager)
    monkeypatch.setattr("lazyagent.app.load_config", lambda repo_root: Config())
    monkeypatch.setattr(
        WorktreePanel,
        "_try_start_terminal",
        lambda self: None,
    )

    def no_pr_refresh(self) -> None:
        return None

    monkeypatch.setattr(LazyAgent, "_refresh_pr_status", no_pr_refresh)


def _select_worktree(app: LazyAgent, index: int) -> WorktreeInfo:
    worktree_list = app.query_one(WorktreeList)
    item = [
        child
        for child in worktree_list.children
        if isinstance(child, WorktreeListItem)
    ][index]
    # +1 to account for the OrchestratorListItem at position 0
    worktree_list.index = index + 1
    app.on_list_view_highlighted(WorktreeList.Highlighted(worktree_list, item))
    return item.worktree


@pytest.mark.asyncio
async def test_switching_worktrees_restores_existing_panel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_app_dependencies(monkeypatch)

    app = LazyAgent(repo_path="/repo")

    async with app.run_test() as pilot:
        first = _select_worktree(app, 0)
        await pilot.pause()

        center = app.query_one(CenterPanel)
        first_panel = center.get_panel(first.path)
        assert first_panel is not None

        first_panel.switch_to_tab("diff-tab")
        first_tabs = first_panel.query_one("#agent-tabs", TabbedContent)
        assert first_tabs.active == "diff-tab"

        second = _select_worktree(app, 1)
        await pilot.pause()

        second_panel = center.get_panel(second.path)
        assert second_panel is not None
        assert second_panel is not first_panel

        _select_worktree(app, 0)
        await pilot.pause()

        assert center.get_panel(first.path) is first_panel
        assert first_panel.query_one("#agent-tabs", TabbedContent).active == "diff-tab"


@pytest.mark.asyncio
async def test_ctrl_j_uses_spawn_flow_when_selected_worktree_has_no_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_app_dependencies(monkeypatch)

    app = LazyAgent(repo_path="/repo")

    async with app.run_test() as pilot:
        _select_worktree(app, 1)
        await pilot.pause()

        app.action_spawn_agent = MagicMock()

        await pilot.press("ctrl+j")
        await pilot.pause()

        app.action_spawn_agent.assert_called_once_with()


def test_do_create_worktree_injects_custom_command_into_terminal() -> None:
    app = _make_app_with_config(
        create="create-worktree {branch} {name} {base} {path} {repo}"
    )

    app._do_create_worktree(
        CreateWorktreeResult(branch="feature/demo", base_branch="main")
    )

    app._send_to_terminal.assert_called_once_with(
        "create-worktree feature/demo repo-feature/demo main /repo-feature/demo /repo"
    )
    app.notify.assert_called_once()
    assert "press r to refresh when done" in app.notify.call_args.args[0]


def test_do_remove_worktree_injects_custom_command_with_repo_cd_prefix() -> None:
    app = _make_app_with_config(remove="remove-worktree {name} {path}")

    app._do_remove_worktree(FEATURE_WORKTREE)

    app._send_to_terminal.assert_called_once_with(
        "cd /repo && remove-worktree repo-feature /repo-feature"
    )
    app.action_focus_terminal.assert_called_once_with()


@pytest.mark.parametrize("status", [AgentStatus.RUNNING, AgentStatus.WAITING])
def test_remove_worktree_is_blocked_for_active_agents(status: AgentStatus) -> None:
    app = LazyAgent(repo_path="/repo")
    app._get_selected_worktree = lambda: FEATURE_WORKTREE
    app._get_agent_state = lambda path: AgentState(status=status)
    app.notify = MagicMock()
    app.push_screen = MagicMock()

    app.action_remove_worktree()

    app.push_screen.assert_not_called()
    app.notify.assert_called_once()
    assert "stop it first" in app.notify.call_args.args[0]


def test_remove_worktree_is_blocked_for_main_worktree() -> None:
    app = LazyAgent(repo_path="/repo")
    app._get_selected_worktree = lambda: MAIN_WORKTREE
    app.notify = MagicMock()
    app.push_screen = MagicMock()

    app.action_remove_worktree()

    app.push_screen.assert_not_called()
    app.notify.assert_called_once()
    assert "Cannot remove the main worktree" in app.notify.call_args.args[0]


# --- RemoveWorktreeResult tests ---


def test_remove_worktree_result_defaults() -> None:
    result = RemoveWorktreeResult(force=False)
    assert result.force is False


def test_remove_worktree_result_force() -> None:
    result = RemoveWorktreeResult(force=True)
    assert result.force is True


def test_do_remove_worktree_passes_force_to_builtin() -> None:
    app = _make_app_with_config()
    mock_manager = MagicMock()
    monkeypatch_manager = MagicMock()
    mock_manager.return_value = monkeypatch_manager
    app._load_worktrees = MagicMock()

    import lazyagent.app as app_module

    original = app_module.WorktreeManager
    app_module.WorktreeManager = mock_manager
    try:
        app._do_remove_worktree(FEATURE_WORKTREE, force=True)
        monkeypatch_manager.remove.assert_called_once_with("/repo-feature", force=True)
    finally:
        app_module.WorktreeManager = original


def test_do_remove_worktree_expands_force_placeholder_in_custom_command() -> None:
    app = _make_app_with_config(remove="remove-worktree {name} {path} {force}")

    app._do_remove_worktree(FEATURE_WORKTREE, force=True)

    app._send_to_terminal.assert_called_once_with(
        "cd /repo && remove-worktree repo-feature /repo-feature --force"
    )


def test_do_remove_worktree_custom_without_force() -> None:
    app = _make_app_with_config(remove="remove-worktree {name} {path} {force}")

    app._do_remove_worktree(FEATURE_WORKTREE, force=False)

    app._send_to_terminal.assert_called_once_with(
        "cd /repo && remove-worktree repo-feature /repo-feature"
    )


# --- CreateWorktreeResult extra_options tests ---


def test_create_worktree_result_extra_options_default() -> None:
    result = CreateWorktreeResult(branch="feat", base_branch="main")
    assert result.extra_options == ""


def test_create_worktree_result_extra_options() -> None:
    result = CreateWorktreeResult(
        branch="feat", base_branch="main", extra_options="--no-build"
    )
    assert result.extra_options == "--no-build"


def test_do_create_worktree_expands_extra_placeholder() -> None:
    app = _make_app_with_config(
        create="create-worktree {branch} {name} {base} {path} {repo} {extra}"
    )

    app._do_create_worktree(
        CreateWorktreeResult(
            branch="feature/demo",
            base_branch="main",
            extra_options="--no-build --skip-hooks",
        )
    )

    app._send_to_terminal.assert_called_once_with(
        "create-worktree feature/demo repo-feature/demo main /repo-feature/demo /repo --no-build --skip-hooks"
    )


def test_do_create_worktree_no_extra_options() -> None:
    app = _make_app_with_config(
        create="create-worktree {branch} {name} {base} {path} {repo} {extra}"
    )

    app._do_create_worktree(
        CreateWorktreeResult(branch="feature/demo", base_branch="main")
    )

    app._send_to_terminal.assert_called_once_with(
        "create-worktree feature/demo repo-feature/demo main /repo-feature/demo /repo"
    )
