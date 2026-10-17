from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.binding import Binding
from textual.widgets import TabbedContent
from textual.worker import WorkerState

from lazyagent.app import LazyAgent
from tests.tui_backends import settle, tui_app
from lazyagent.config import Config, WorktreeConfig
from lazyagent.engine.engine import CreatedWorktree
from lazyagent.models import AgentState, AgentStatus, GitStatus, PrInfo, WorktreeInfo
from lazyagent.worktree_manager import WorktreeManagerError
from lazyagent.widgets.center_panel import CenterPanel, WorktreePanel
from lazyagent.widgets.create_worktree_modal import CreateWorktreeResult
from lazyagent.widgets.job_log import JobLog
from lazyagent.widgets.pr_status_bar import PrStatusBar
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

# Captured before any test stubs it out in _patch_app_dependencies.
_REAL_REFRESH_PR_STATUS = LazyAgent._refresh_pr_status


def _make_app_with_config(
    create: str | None = None,
    remove: str | None = None,
) -> LazyAgent:
    """Create a LazyAgent with custom worktree config and mocked I/O.

    The tests that use it mock the Backend calls the app makes, so they say
    nothing about which backend runs (and need no pilot): they cover both.
    """
    app = LazyAgent(repo_path="/repo")
    app.backend.engine.worktree_service.repo_root = "/repo"
    app.backend.engine.config = Config(worktree=WorktreeConfig(create=create, remove=remove))
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
    monkeypatch.setattr("lazyagent.engine.engine.WorktreeManager", DummyWorktreeManager)
    monkeypatch.setattr("lazyagent.engine.engine.load_config", lambda repo_root: Config())
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
        # The full git sweep is the engine's own background work; a remote
        # app's requests reach the engine before the sync's, and the events
        # they cause reach the app before it returns.
        await app.backend.sync()
        await asyncio.wait_for(app.backend.engine.wait_idle(), 5.0)
        await app.backend.sync()
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
    monkeypatch: pytest.MonkeyPatch, backend_kind: str,
) -> None:
    _patch_app_dependencies(monkeypatch)

    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        first = await _select_worktree(app, 0)
        await settle(pilot)

        center = app.query_one(CenterPanel)
        first_panel = center.get_panel(first.path)
        assert first_panel is not None

        first_panel.switch_to_tab("diff-tab")
        first_tabs = first_panel.query_one("#agent-tabs", TabbedContent)
        assert first_tabs.active == "diff-tab"

        second = await _select_worktree(app, 1)
        await settle(pilot)

        second_panel = center.get_panel(second.path)
        assert second_panel is not None
        assert second_panel is not first_panel

        await _select_worktree(app, 0)
        await settle(pilot)

        assert center.get_panel(first.path) is first_panel
        assert first_panel.query_one("#agent-tabs", TabbedContent).active == "diff-tab"


@pytest.mark.asyncio
async def test_ctrl_j_uses_spawn_flow_when_selected_worktree_has_no_agent(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str,
) -> None:
    _patch_app_dependencies(monkeypatch)

    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        await _select_worktree(app, 1)
        await settle(pilot)

        app.action_spawn_agent = MagicMock()

        await pilot.press("ctrl+j")
        await settle(pilot)

        app.action_spawn_agent.assert_called_once_with()


@pytest.mark.asyncio
async def test_do_create_worktree_asks_the_backend_and_selects_the_result() -> None:
    """The command itself (custom or not) is the engine's: see
    tests/engine/test_engine.py::TestWorktreeJobs."""
    app = _make_app_with_config(create="create-worktree {branch}")
    app.backend.create_worktree = AsyncMock(
        return_value=CreatedWorktree("/repo-feature/demo", "feature/demo", "j1")
    )
    app._queue_selection = MagicMock()

    await app._do_create_worktree(
        CreateWorktreeResult(
            branch="feature/demo", base_branch="main", extra_options="--no-build"
        )
    )

    app.backend.create_worktree.assert_awaited_once_with(
        "feature/demo", "main", "--no-build", env=dict(os.environ)
    )
    app._queue_selection.assert_called_once_with("/repo-feature/demo")
    assert "Created worktree: demo" in app.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_do_create_worktree_reports_a_failure() -> None:
    app = _make_app_with_config(create="create-worktree {branch}")
    app.backend.create_worktree = AsyncMock(
        side_effect=WorktreeManagerError("Custom create command failed: boom")
    )

    await app._do_create_worktree(CreateWorktreeResult(branch="feat", base_branch="main"))

    assert app.notify.call_args.kwargs["severity"] == "error"
    assert "boom" in app.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_do_remove_worktree_asks_the_backend() -> None:
    app = _make_app_with_config(remove="remove-worktree {name} {path} {force}")
    app.backend.remove_worktree = AsyncMock(return_value="j1")
    app.backend.engine.worktree_service.worktrees = [MAIN_WORKTREE]
    app._queue_selection = MagicMock()

    await app._do_remove_worktree(FEATURE_WORKTREE, force=True)

    # Jobs started here run with this client's environment (D5).
    app.backend.remove_worktree.assert_awaited_once_with(
        "/repo-feature", True, env=dict(os.environ)
    )
    assert "Removed worktree: repo-feature" in app.notify.call_args.args[0]
    # The first item: removing the selected worktree leaves nothing to keep.
    app._queue_selection.assert_called_once_with(None)


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


# --- CreateWorktreeResult extra_options tests ---


def test_create_worktree_result_extra_options_default() -> None:
    result = CreateWorktreeResult(branch="feat", base_branch="main")
    assert result.extra_options == ""


def test_create_worktree_result_extra_options() -> None:
    result = CreateWorktreeResult(
        branch="feat", base_branch="main", extra_options="--no-build"
    )
    assert result.extra_options == "--no-build"


# ---------------------------------------------------------------------------
# Git status refresh
# ---------------------------------------------------------------------------


def _status_paths() -> set[str]:
    return {path for kind, path in DummyWorktreeManager.calls if kind == "status"}


class TestGitStatusRefresh:
    @pytest.mark.asyncio
    async def test_periodic_poll_only_touches_the_selected_worktree(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """The timer must not sweep every worktree — that is the expensive part."""
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            app._refresh_selected_git_status()
            await _drain_workers(app)

        assert _status_paths() == {wt.path}

    @pytest.mark.asyncio
    async def test_full_rescan_still_covers_every_worktree(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """`r` / create / remove / MCP still refresh the whole sidebar."""
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            app._load_worktrees()
            await _drain_workers(app)

            assert set(app._git_statuses) == {wt.path for wt in WORKTREES}
        assert _status_paths() == {wt.path for wt in WORKTREES}

    @pytest.mark.asyncio
    async def test_sweep_does_not_block_the_message_pump(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """The regression guard: `git status` runs off the event loop.

        Sweeping every worktree took ~3 s on a repo with 22 of them, and it
        used to run inline — freezing the whole UI twice a minute. If the
        `@work(thread=True)` decorator is ever dropped, this call blocks for
        the full delay and the test fails.
        """
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

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
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """Merging, not replacing: the sidebar must not lose the other badges."""
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            other = next(w for w in WORKTREES if w.path != wt.path)
            app._git_statuses = {
                other.path: GitStatus(last_commit_subject="old other"),
                wt.path: GitStatus(last_commit_subject="old selected"),
            }

            app._refresh_selected_git_status()
            await _drain_workers(app)

            assert app._git_statuses[wt.path].last_commit_subject == (
                f"commit for {wt.path}"
            )
            assert app._git_statuses[other.path].last_commit_subject == (
                "old other"
            )

    @pytest.mark.asyncio
    async def test_action_refreshes_only_git_status(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """Alt+G refreshes the badge without re-listing worktrees."""
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

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
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)

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
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        # Long window so it cannot fire mid-test: what matters is that passing
        # through *queues* nothing, not how long the window happens to be.
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            # Scroll past the first worktree and land on the second.
            await _select_worktree(app, 0)
            passed_through = app._selection_debounce
            final = await _select_worktree(app, 1)

            # Neither worktree has been fetched yet...
            assert _status_paths() == set()
            # ...and the worktree we passed through had its pending fetch
            # replaced rather than a second one queued alongside it.
            assert passed_through is not None
            assert app._selection_debounce is not passed_through

            # Firing what the timer would call fetches only where we landed.
            app._refresh_selected_git_status()
            await _drain_workers(app)

        assert _status_paths() == {final.path}

    @pytest.mark.asyncio
    async def test_staying_on_a_worktree_polls_it(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """The timer really does fire once the selection settles."""
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 0.05)
        app = tui_app(backend_kind)

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            wt = await _select_worktree(app, 1)
            # Until the timer has fired and its fetch ran: on a loaded machine
            # a 50 ms timer can fire well after any fixed sleep.
            deadline = time.perf_counter() + 30.0
            while not _status_paths():
                assert time.perf_counter() < deadline, "the timer never fetched"
                await asyncio.sleep(0.01)
            await _drain_workers(app)

        assert _status_paths() == {wt.path}

    @pytest.mark.asyncio
    async def test_leaving_for_the_orchestrator_cancels_the_pending_poll(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)

        async with app.run_test():
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()

            await _select_worktree(app, 1)
            assert app._selection_debounce is not None

            worktree_list = app.query_one(WorktreeList)
            orchestrator = next(
                child for child in worktree_list.children
                if isinstance(child, OrchestratorListItem)
            )
            await app.on_list_view_highlighted(
                WorktreeList.Highlighted(worktree_list, orchestrator)
            )

            assert app._selection_debounce is None
            await _drain_workers(app)

        assert _status_paths() == set()

    @pytest.mark.asyncio
    async def test_on_demand_refresh_is_not_debounced(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """Alt+G is an explicit request — it fires immediately."""
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)

        async with app.run_test():
            wt = await _select_worktree(app, 1)
            await _drain_workers(app)
            DummyWorktreeManager.calls.clear()
            app.notify = MagicMock()

            app.action_refresh_git_status()
            await _drain_workers(app)

            # Fired without waiting out the window.
            assert _status_paths() == {wt.path}
            # The pending selection refresh is left alone: it also refreshes
            # the diff, which this shortcut deliberately does not.
            assert app._selection_debounce is not None


class TestPrStatusDebounce:
    """The ``gh`` lookup on selection is throttled like git status.

    A burst of them would also hold the ``asyncio.to_thread`` pool the git
    fetches run on.
    """

    @staticmethod
    def _record_pr_lookups(
        monkeypatch: pytest.MonkeyPatch, app: LazyAgent
    ) -> list[list[str]]:
        monkeypatch.setattr(LazyAgent, "_refresh_pr_status", _REAL_REFRESH_PR_STATUS)
        lookups: list[list[str]] = []

        async def refresh_pr_info(paths) -> None:
            lookups.append(list(paths))

        monkeypatch.setattr(app.backend.engine.worktree_service, "refresh_pr_info", refresh_pr_info)
        return lookups

    @pytest.mark.asyncio
    async def test_passing_through_worktrees_looks_up_no_pr(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)
        lookups = self._record_pr_lookups(monkeypatch, app)

        async with app.run_test():
            await _drain_workers(app)

            await _select_worktree(app, 1)
            await _select_worktree(app, 0)
            await _select_worktree(app, 1)
            await _drain_workers(app)

            assert lookups == []
            assert app._selection_debounce is not None

    @pytest.mark.asyncio
    async def test_settling_looks_up_the_pr_once(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)
        lookups = self._record_pr_lookups(monkeypatch, app)

        async with app.run_test():
            await _drain_workers(app)

            await _select_worktree(app, 1)
            await _select_worktree(app, 0)
            final = await _select_worktree(app, 1)

            # Firing what the timer would call looks up only where we landed.
            app._run_selection_refresh()
            await _drain_workers(app)

        assert lookups == [[final.path]]

    @pytest.mark.asyncio
    async def test_leaving_for_the_orchestrator_cancels_the_pr_lookup(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)
        lookups = self._record_pr_lookups(monkeypatch, app)

        async with app.run_test():
            await _drain_workers(app)

            await _select_worktree(app, 1)
            worktree_list = app.query_one(WorktreeList)
            orchestrator = next(
                child
                for child in worktree_list.children
                if isinstance(child, OrchestratorListItem)
            )
            await app.on_list_view_highlighted(
                WorktreeList.Highlighted(worktree_list, orchestrator)
            )
            await _drain_workers(app)

            assert app._selection_debounce is None

        assert lookups == []

    @pytest.mark.asyncio
    async def test_cached_badge_is_shown_without_waiting(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)
        lookups = self._record_pr_lookups(monkeypatch, app)
        shown: list[PrInfo | None] = []
        monkeypatch.setattr(
            PrStatusBar, "update_pr_info", lambda self, info: shown.append(info)
        )
        cached = PrInfo(number=7, title="Demo", state="OPEN", checks=[])
        # Cached before the app starts: a remote app gets it with the
        # engine's state when it connects.
        app.backend.engine.worktree_service._pr_infos[FEATURE_WORKTREE.path] = cached

        async with app.run_test():
            await _drain_workers(app)

            await _select_worktree(app, 1)

            assert shown and shown[-1] == cached
            # The badge came from the cache: the lookup is still pending.
            assert lookups == []
            assert app._selection_debounce is not None


class TestLateDiff:
    @pytest.mark.asyncio
    async def test_a_diff_that_outlives_the_screen_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        """A diff fetch that returns while the app shuts down (its screen
        already gone) must not fail the worker, which fails the app's exit."""
        _patch_app_dependencies(monkeypatch)
        app = tui_app(backend_kind)
        async with app.run_test():
            await _drain_workers(app)
            wt = await _select_worktree(app, 1)
            await app.query_one(CenterPanel).remove()
            app._apply_diff(wt.path, "diff --git a/x b/x")  # no NoMatches


class TestDiffRefreshDebounce:
    """The diff refresh is throttled like git status.

    It costs three git subprocesses, and its result is parsed and measured on
    the main thread, so firing one per worktree scrolled past is expensive at
    both ends.
    """

    @pytest.mark.asyncio
    async def test_diff_is_not_fetched_while_scrolling(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 30.0)
        app = tui_app(backend_kind)
        diffs: list[str] = []
        monkeypatch.setattr(
            DummyWorktreeManager, "get_diff",
            staticmethod(lambda path: diffs.append(path) or ""),
        )

        async with app.run_test():
            await _drain_workers(app)
            diffs.clear()

            await _select_worktree(app, 0)
            await _select_worktree(app, 1)
            await _drain_workers(app)

        assert diffs == []

    @pytest.mark.asyncio
    async def test_diff_is_fetched_once_the_selection_settles(
        self, monkeypatch: pytest.MonkeyPatch, backend_kind: str
    ) -> None:
        _patch_app_dependencies(monkeypatch)
        monkeypatch.setattr("lazyagent.app._SELECTION_DEBOUNCE", 0.05)
        app = tui_app(backend_kind)
        diffs: list[str] = []
        monkeypatch.setattr(
            DummyWorktreeManager, "get_diff",
            staticmethod(lambda path: diffs.append(path) or ""),
        )

        async with app.run_test():
            await _drain_workers(app)
            diffs.clear()

            wt = await _select_worktree(app, 1)
            # Until the timer has fired and its fetch ran: on a loaded machine
            # a short timer can fire well after any fixed sleep.
            deadline = time.perf_counter() + 30.0
            while not diffs:
                assert time.perf_counter() < deadline, "the timer never fetched"
                await asyncio.sleep(0.01)
            await _drain_workers(app)

        # A set, not a list: _select_worktree both awaits the handler directly
        # and lets Textual deliver the posted Highlighted message, so the
        # harness itself selects twice. What matters is that the settled
        # worktree — and only it — was fetched.
        assert diffs and set(diffs) == {wt.path}


@pytest.mark.asyncio
async def test_a_created_worktree_is_selected_once_the_sidebar_is_rebuilt(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str,
) -> None:
    """The rebuild is asynchronous: selecting among the children before it
    settles left the index past the end of the list, and the next cursor key
    crashed the app."""
    _patch_app_dependencies(monkeypatch)
    worktrees = list(WORKTREES)
    monkeypatch.setattr(DummyWorktreeManager, "list", lambda self: list(worktrees))
    new = WorktreeInfo(
        path="/repo-new", head="c" * 40, branch="new", is_main=False, is_bare=False
    )
    app = tui_app(backend_kind)

    async def create(branch, base_branch="main", extra="", **kwargs):
        worktrees.append(new)
        app.backend.engine.refresh_worktrees()
        return CreatedWorktree(new.path, branch)

    async with app.run_test() as pilot:
        await _select_worktree(app, 1)
        await settle(pilot)
        app.backend.engine.create_worktree = create
        app.notify = MagicMock()

        await app._do_create_worktree(CreateWorktreeResult(branch="new", base_branch="main"))
        await _drain_workers(app)
        await settle(pilot)

        wt_list = app.query_one(WorktreeList)
        assert len(wt_list.children) == 4
        assert wt_list.highlighted_child.worktree.path == new.path
        await pilot.press("k")
        await settle(pilot)
        assert wt_list.highlighted_child.worktree.path == FEATURE_WORKTREE.path


@pytest.mark.asyncio
async def test_job_output_shows_under_the_worktree_panel(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str,
) -> None:
    _patch_app_dependencies(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test(size=(120, 40)) as pilot:
        await _select_worktree(app, 1)
        await settle(pilot)

        await app.backend.engine.run_job("echo from the job", title="Create worktree x")
        await settle(pilot)

        log = app.query_one(JobLog)
        center = app.query_one(CenterPanel)
        assert log.display
        assert "from the job" in "\n".join(log.lines)
        # Inside the center panel, not pushed below it by the worktree panel.
        assert center.region.contains_region(log.region)


@pytest.mark.asyncio
@pytest.mark.parametrize("index", [0, 1])
async def test_refresh_keeps_the_selected_worktree(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str, index: int
) -> None:
    """``r`` rebuilds the sidebar; the selection used to be lost with it."""
    _patch_app_dependencies(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        selected = await _select_worktree(app, index)
        await settle(pilot)
        app.notify = MagicMock()

        await pilot.press("r")
        await _drain_workers(app)
        await settle(pilot)

        assert app._get_selected_worktree() == selected
        # The index is valid again: the cursor moves on from there.
        await pilot.press("j")
        await settle(pilot)
        assert app._get_selected_worktree() == WORKTREES[min(index + 1, len(WORKTREES) - 1)]


@pytest.mark.asyncio
async def test_refresh_keeps_the_orchestrator_selected(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str,
) -> None:
    _patch_app_dependencies(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        wt_list = app.query_one(WorktreeList)
        orchestrator = next(
            c for c in wt_list.children if isinstance(c, OrchestratorListItem)
        )
        wt_list.index = 0
        await app.on_list_view_highlighted(WorktreeList.Highlighted(wt_list, orchestrator))
        await settle(pilot)

        app.action_refresh()
        await _drain_workers(app)
        await settle(pilot)

        assert isinstance(wt_list.highlighted_child, OrchestratorListItem)
        assert app._orchestrator_selected


@pytest.mark.asyncio
async def test_an_agent_out_of_sight_does_not_take_the_keyboard(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str
) -> None:
    """An agent spawned in a worktree not on screen (over MCP, or already
    running when a remote TUI connects) gets its tab but not the focus."""
    from lazyagent.widgets.monitored_terminal import MonitoredTerminal
    from tests.engine.fakes import no_pty

    _patch_app_dependencies(monkeypatch)
    no_pty(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        await _select_worktree(app, 0)
        await settle(pilot)
        app.query_one(WorktreeList).focus()
        engine = app.backend.engine

        await engine.spawn_agent(FEATURE_WORKTREE.path)
        await settle(pilot)
        panel = app.query_one(CenterPanel).get_panel(FEATURE_WORKTREE.path)
        assert panel is not None and panel.agent_ids == ["a1"]
        assert isinstance(app.focused, WorktreeList)

        await engine.spawn_agent(MAIN_WORKTREE.path)
        await settle(pilot)
        assert isinstance(app.focused, MonitoredTerminal)
        assert app.focused.session.worktree_path == MAIN_WORKTREE.path


@pytest.mark.asyncio
async def test_two_quick_spawns_leave_the_newer_agent_focused(
    monkeypatch: pytest.MonkeyPatch, backend_kind: str
) -> None:
    """Focus is deferred in Textual: the first agent's focus, queued behind
    the second agent's tab, must not switch back to the first tab."""
    from lazyagent.widgets.monitored_terminal import MonitoredTerminal
    from tests.engine.fakes import no_pty

    _patch_app_dependencies(monkeypatch)
    no_pty(monkeypatch)
    app = tui_app(backend_kind)

    async with app.run_test() as pilot:
        await _select_worktree(app, 1)
        await settle(pilot)
        engine = app.backend.engine
        await engine.spawn_agent(FEATURE_WORKTREE.path)
        await engine.spawn_agent(FEATURE_WORKTREE.path)
        await settle(pilot)
        await settle(pilot)

        panel = app.query_one(CenterPanel).get_panel(FEATURE_WORKTREE.path)
        assert panel.shown_agent_id == "a2"
        assert isinstance(app.focused, MonitoredTerminal)
        assert app.focused.session.agent_id == "a2"
