from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.binding import Binding
from textual.widgets import TabbedContent
from textual.worker import WorkerState

from lazyagent.app import LazyAgent
from lazyagent.config import Config, WorktreeConfig
from lazyagent.models import AgentState, AgentStatus, GitStatus, WorktreeInfo
from lazyagent.widgets.center_panel import CenterPanel, WorktreePanel
from lazyagent.widgets.create_worktree_modal import CreateWorktreeResult
from lazyagent.widgets.remove_worktree_modal import RemoveWorktreeResult
from lazyagent.widgets.worktree_list import (
    OrchestratorListItem,
    WorktreeList,
    WorktreeListItem,
)


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
    #: (kind, path) for every git call, so tests can assert *which* worktrees
    #: were touched. Class-level: the app builds its own manager instances.
    calls: list[tuple[str, str]] = []
    #: seconds each git call blocks for — used to prove the work is threaded
    delay: float = 0.0

    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path)

    def list(self) -> list[WorktreeInfo]:
        return WORKTREES

    def get_all_git_statuses(
        self, worktrees: list[WorktreeInfo]
    ) -> dict[str, GitStatus]:
        for wt in worktrees:
            DummyWorktreeManager.calls.append(("status", wt.path))
        if DummyWorktreeManager.delay:
            time.sleep(DummyWorktreeManager.delay)
        return {
            wt.path: GitStatus(last_commit_subject=f"commit for {wt.name}")
            for wt in worktrees
        }

    def get_git_status(self, worktree_path: str | Path) -> GitStatus:
        DummyWorktreeManager.calls.append(("status", str(worktree_path)))
        if DummyWorktreeManager.delay:
            time.sleep(DummyWorktreeManager.delay)
        return GitStatus()

    def get_last_commit_subject(self, worktree_path: str | Path) -> str:
        DummyWorktreeManager.calls.append(("subject", str(worktree_path)))
        return f"commit for {worktree_path}"

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


async def _drain_workers(app: LazyAgent) -> None:
    """Wait for background workers to settle.

    Not ``workers.wait_for_complete()``: these workers use ``exclusive=True``,
    and that helper re-raises ``WorkerCancelled`` for every worker an exclusive
    call superseded — which is normal operation here, not a failure.
    """
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        if not [
            w for w in app.workers
            if w.state in (WorkerState.PENDING, WorkerState.RUNNING)
        ]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("workers did not finish within 5s")


async def _select_worktree(app: LazyAgent, index: int) -> WorktreeInfo:
    worktree_list = app.query_one(WorktreeList)
    item = [
        child
        for child in worktree_list.children
        if isinstance(child, WorktreeListItem)
    ][index]
    # +1 to account for the OrchestratorListItem at position 0
    worktree_list.index = index + 1
    await app.on_list_view_highlighted(WorktreeList.Highlighted(worktree_list, item))
    return item.worktree


@pytest.mark.asyncio
async def test_switching_worktrees_restores_existing_panel_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_app_dependencies(monkeypatch)

    app = LazyAgent(repo_path="/repo")

    async with app.run_test() as pilot:
        first = await _select_worktree(app, 0)
        await pilot.pause()

        center = app.query_one(CenterPanel)
        first_panel = center.get_panel(first.path)
        assert first_panel is not None

        first_panel.switch_to_tab("diff-tab")
        first_tabs = first_panel.query_one("#agent-tabs", TabbedContent)
        assert first_tabs.active == "diff-tab"

        second = await _select_worktree(app, 1)
        await pilot.pause()

        second_panel = center.get_panel(second.path)
        assert second_panel is not None
        assert second_panel is not first_panel

        await _select_worktree(app, 0)
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
        await _select_worktree(app, 1)
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
    app._agent_states = {
        FEATURE_WORKTREE.path: {"a1": AgentState(status=status, agent_id="a1")}
    }
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


# ---------------------------------------------------------------------------
# Git status refresh
# ---------------------------------------------------------------------------


def _status_paths() -> set[str]:
    return {path for kind, path in DummyWorktreeManager.calls if kind == "status"}


class TestGitStatusRefresh:
    @pytest.mark.asyncio
    async def test_periodic_poll_only_touches_the_selected_worktree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The timer must not sweep every worktree — that is the expensive part."""
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            app._refresh_selected_git_status()
            await _drain_workers(app)

        assert _status_paths() == {wt.path}

    @pytest.mark.asyncio
    async def test_full_rescan_still_covers_every_worktree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`r` / create / remove / MCP still refresh the whole sidebar."""
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            app._load_worktrees()
            await _drain_workers(app)

            assert set(app._git_statuses) == {wt.path for wt in WORKTREES}
        assert _status_paths() == {wt.path for wt in WORKTREES}

    @pytest.mark.asyncio
    async def test_sweep_does_not_block_the_message_pump(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression guard: `git status` runs off the event loop.

        Sweeping every worktree took ~3 s on a repo with 22 of them, and it
        used to run inline — freezing the whole UI twice a minute. If the
        `@work(thread=True)` decorator is ever dropped, this call blocks for
        the full delay and the test fails.
        """
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.delay = 0.3
            try:
                start = time.perf_counter()
                app._refresh_git_statuses()
                inline = time.perf_counter() - start

                start = time.perf_counter()
                app._refresh_selected_git_status()
                inline_selected = time.perf_counter() - start

                await _drain_workers(app)
            finally:
                DummyWorktreeManager.delay = 0.0

        assert inline < 0.1, f"full sweep blocked the pump for {inline:.2f}s"
        assert inline_selected < 0.1, (
            f"selected refresh blocked the pump for {inline_selected:.2f}s"
        )

    @pytest.mark.asyncio
    async def test_selected_refresh_keeps_other_statuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Merging, not replacing: the sidebar must not lose the other badges."""
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            app._git_statuses = {
                MAIN_WORKTREE.path: GitStatus(last_commit_subject="old main"),
                FEATURE_WORKTREE.path: GitStatus(last_commit_subject="old feature"),
            }

            app._apply_git_statuses(
                {FEATURE_WORKTREE.path: GitStatus(last_commit_subject="new feature")}
            )

            assert app._git_statuses[FEATURE_WORKTREE.path].last_commit_subject == (
                "new feature"
            )
            assert app._git_statuses[MAIN_WORKTREE.path].last_commit_subject == (
                "old main"
            )

    @pytest.mark.asyncio
    async def test_action_refreshes_only_git_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alt+G refreshes the badge without re-listing worktrees."""
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()
            app.notify = MagicMock()
            reload_worktrees = MagicMock()
            monkeypatch.setattr(LazyAgent, "_load_worktrees", reload_worktrees)

            app.action_refresh_git_status()
            await _drain_workers(app)

        assert _status_paths() == {wt.path}
        reload_worktrees.assert_not_called()

    @pytest.mark.asyncio
    async def test_action_is_a_noop_without_a_selection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            app._selected_worktree = None
            DummyWorktreeManager.calls.clear()

            app.action_refresh_git_status()
            await _drain_workers(app)

        assert DummyWorktreeManager.calls == []

    def test_binding_is_registered(self) -> None:
        bindings = {
            b.key: b.action
            for b in LazyAgent.BINDINGS
            if isinstance(b, Binding)
        }
        assert bindings["alt+g"] == "refresh_git_status"
        assert next(
            b for b in LazyAgent.BINDINGS
            if isinstance(b, Binding) and b.key == "alt+g"
        ).priority is True


class TestGitStatusDebounce:
    """Moving through the sidebar must not fetch status for every worktree."""

    @pytest.mark.asyncio
    async def test_passing_through_worktrees_only_polls_the_final_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        # Long window so it cannot fire mid-test: what matters is that passing
        # through *queues* nothing, not how long the window happens to be.
        monkeypatch.setattr("lazyagent.app._GIT_STATUS_DEBOUNCE", 30.0)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            # Scroll past the first worktree and land on the second.
            await _select_worktree(app, 0)
            passed_through = app._git_status_debounce
            final = await _select_worktree(app, 1)

            # Neither worktree has been fetched yet...
            assert _status_paths() == set()
            # ...and the worktree we passed through had its pending fetch
            # replaced rather than a second one queued alongside it.
            assert passed_through is not None
            assert app._git_status_debounce is not passed_through

            # Firing what the timer would call fetches only where we landed.
            app._refresh_selected_git_status()
            await _drain_workers(app)

        assert _status_paths() == {final.path}

    @pytest.mark.asyncio
    async def test_staying_on_a_worktree_polls_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The timer really does fire once the selection settles."""
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._GIT_STATUS_DEBOUNCE", 0.05)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            wt = await _select_worktree(app, 1)
            await asyncio.sleep(0.4)
            await _drain_workers(app)

        assert _status_paths() == {wt.path}

    @pytest.mark.asyncio
    async def test_leaving_for_the_orchestrator_cancels_the_pending_poll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._GIT_STATUS_DEBOUNCE", 30.0)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            await _select_worktree(app, 1)
            assert app._git_status_debounce is not None

            worktree_list = app.query_one(WorktreeList)
            orchestrator = next(
                child for child in worktree_list.children
                if isinstance(child, OrchestratorListItem)
            )
            await app.on_list_view_highlighted(
                WorktreeList.Highlighted(worktree_list, orchestrator)
            )

            assert app._git_status_debounce is None
            await _drain_workers(app)

        assert _status_paths() == set()

    @pytest.mark.asyncio
    async def test_on_demand_refresh_is_not_debounced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alt+G is an explicit request — it fires immediately."""
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._GIT_STATUS_DEBOUNCE", 30.0)
        app = LazyAgent(repo_path="/repo")

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()
            app.notify = MagicMock()

            app.action_refresh_git_status()
            await _drain_workers(app)

            # Fired without waiting out the window...
            assert _status_paths() == {wt.path}
            # ...and dropped the pending selection-triggered fetch.
            assert app._git_status_debounce is None
