from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from functools import partial
from typing import Any

from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Footer, Header, TabbedContent

from lazyagent import diagnostics as diag
from lazyagent.backend import (
    BACKEND_CONNECTED,
    BACKEND_DISCONNECTED,
    Backend,
    EngineUnavailable,
    make_backend,
)
from lazyagent.engine.events import (
    AGENT_EXITED,
    AGENT_SPAWNED,
    AGENT_STATUS,
    AGENT_UPDATED,
    ENGINE_NOTICE,
    GIT_STATUS_UPDATED,
    JOB_FINISHED,
    JOB_OUTPUT,
    JOB_STARTED,
    PR_INFO_UPDATED,
    UI_COMMAND,
    WORKTREES_CHANGED,
    EngineEvent,
)
from lazyagent.engine.registry import AgentRegistry
from lazyagent.engine.types import ORCHESTRATOR_KIND
from lazyagent.models import AgentState, GitStatus, WorktreeInfo, default_base
from lazyagent.protocol.hosted import encode_osc, host, tmux_passthrough
from lazyagent.widgets.center_panel import CenterPanel, WorktreePanel, pr_diff_text
from lazyagent.widgets.confirm_modal import ConfirmModal
from lazyagent.widgets.help_modal import HelpModal
from lazyagent.widgets.job_log import JobLog
from lazyagent.widgets.create_worktree_modal import CreateWorktreeModal, CreateWorktreeResult
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.remove_worktree_modal import RemoveWorktreeModal, RemoveWorktreeResult
from lazyagent.widgets.pr_status_bar import PrStatusBar
from lazyagent.widgets.prompt_modal import SpawnModal, SpawnResult
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal
from lazyagent.widgets.worktree_list import OrchestratorListItem, WorktreeList, WorktreeListItem
from lazyagent.worktree_manager import WorktreeManagerError


log = logging.getLogger(__name__)


# How long the selection has to settle before we fetch its git status.
# Scrolling the sidebar with j/k should not spawn a `git status` per worktree
# passed through — only the one actually landed on.
_SELECTION_DEBOUNCE = 0.4

_JOB_EVENTS = (JOB_STARTED, JOB_OUTPUT, JOB_FINISHED)

# How long a host's command waits for the worktree it targets to be shown.
_UI_SELECT_TIMEOUT = 5.0

# How long the view has to stay put before ui.view_changed announces it: a
# selection mounts a panel and then moves the focus, and the host should see
# where that ends, not each step.
_VIEW_SETTLE = 0.05

# What ui.view_changed calls each modal. Subclasses first: RemoveWorktreeModal
# is a ConfirmModal.
_MODAL_NAMES: tuple[tuple[type, str], ...] = (
    (SpawnModal, "spawn"),
    (CreateWorktreeModal, "create"),
    (RemoveWorktreeModal, "remove"),
    (ConfirmModal, "confirm"),
    (HelpModal, "help"),
)

# The selection key while nothing is shown (None is the orchestrator).
_NOTHING = object()
# What the center shows after ui.blank.
_BLANK_TEXT = "Shown in the browser. Pick a worktree here to show it in this pane too."


class LazyAgent(App):
    """Textual TUI for managing coding agents across git worktrees.

    A client of the engine through a :class:`~lazyagent.backend.Backend`
    (in-process by default, or a remote engine): keys call the backend, and
    the screen follows the engine's events.
    """

    TITLE = "lazyagent"

    CSS = """
    Screen {
        layout: horizontal;
    }
    Header {
        dock: top;
        height: 1;
        background: $boost;
        color: $text;
    }
    Footer {
        dock: bottom;
        height: 1;
        background: $boost;
    }
    #sidebar {
        dock: left;
        width: 38;
        layout: vertical;
    }
    /* Hosted mode: the host paints the worktree list, PR and git status, and
       has its own chrome, so only the terminals stay. The sidebar is kept in
       the DOM, hidden, because selection still runs through it. */
    LazyAgent.-hosted #sidebar,
    LazyAgent.-hosted Header,
    LazyAgent.-hosted Footer,
    LazyAgent.-hosted GitInfoBar {
        display: none;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "spawn_agent", "Spawn"),
        Binding("x", "stop_agent", "Stop"),
        Binding("c", "create_worktree", "Create"),
        Binding("d", "remove_worktree", "Remove"),
        Binding("ctrl+k", "focus_sidebar", "Ctrl+K Sidebar", priority=True),
        Binding("ctrl+j", "focus_agent", "Ctrl+J Agent", priority=True),
        Binding("ctrl+d", "focus_diff", "Ctrl+D Diff", priority=True),
        Binding("ctrl+l", "focus_terminal", "Ctrl+L Terminal", priority=True),
        # Cycle agent tabs. Alt+] / Alt+[ are the primary keys; on terminals
        # without the Kitty keyboard protocol the Alt modifier is dropped from
        # bracket keys (they arrive as a bare `]`/`[`), so Alt+n / Alt+p are
        # bound as universal fallbacks that keep the modifier everywhere.
        Binding("alt+right_square_bracket,alt+n", "next_agent", "Next agent", priority=True),
        Binding("alt+left_square_bracket,alt+p", "prev_agent", "Prev agent", priority=True),
        # Git status only — no worktree re-list. priority=True so it works
        # while a terminal pane has focus, which is exactly when you want it
        # (you just committed and want the badge to catch up).
        Binding("alt+g", "refresh_git_status", "Git status", priority=True),
        Binding("question_mark", "help", "Help"),
        Binding("R", "restore_agents", "Restore agents", show=False),
    ]

    def __init__(
        self, repo_path: str | None = None, *, backend: Backend | None = None
    ) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.backend = backend if backend is not None else make_backend(repo_path)
        self._unsubscribe_engine: Callable[[], None] | None = None
        self._selected_worktree: WorktreeInfo | None = None
        self._orchestrator_selected: bool = False
        # The engine whose restorable agents were already offered.
        self._restore_offered_for: str | None = None
        self._selection_debounce: Timer | None = None
        # What the center panel shows once a selection has been applied:
        # a worktree path, None for the orchestrator, or _NOTHING.
        self._shown: object = _NOTHING
        # Hosted mode (LAZYAGENT_HOST): the host's identifier, opaque here.
        self.host = host(os.environ)
        self._detach_ui: Callable[[], None] | None = None
        self._ui_commands: asyncio.Queue[dict] | None = None
        self._view_timer: Timer | None = None
        # >0 while a host command runs: its steps are announced as one view.
        self._view_hold = 0
        if self.host is not None:
            self.add_class("-hosted")

    # Views of engine state, for tests and the widgets.

    @property
    def registry(self) -> AgentRegistry:
        return self.backend.registry

    @property
    def _agent_states(self) -> dict[str, dict[str, AgentState]]:
        return self.backend.registry.states

    @_agent_states.setter
    def _agent_states(self, states: dict[str, dict[str, AgentState]]) -> None:
        self.backend.registry.states = states

    @property
    def worktrees(self) -> list[WorktreeInfo]:
        return self.backend.worktrees

    @property
    def _git_statuses(self) -> dict[str, GitStatus]:
        return self.backend.git_statuses

    @_git_statuses.setter
    def _git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        self.backend.git_statuses = statuses

    @property
    def _repo_root(self) -> str:
        return self.backend.repo_root

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar"):
            worktree_list = WorktreeList()
            # Hidden in hosted mode: it must not hold the keyboard either.
            worktree_list.can_focus = self.host is None
            yield worktree_list
            yield PrStatusBar(id="pr-status-bar")
        yield CenterPanel(backend=self.backend)
        yield Footer()

    async def on_mount(self) -> None:
        diag.setup()
        # Before the start, which already reports the worktree list, and
        # which advertises the ui.* commands to v1 clients when a UI is
        # attached.
        self._unsubscribe_engine = self.backend.subscribe(self._on_engine_event)
        if self.host is not None:
            self._ui_commands = asyncio.Queue()
            self._detach_ui = self.backend.attach_ui()
            self.run_worker(self._run_ui_commands, group="ui_commands")
        with diag.timed(log, "app.on_mount") as span:
            await self.backend.start()
            span["worktrees"] = len(self.worktrees)
            span["repo_root"] = self._repo_root
        if self.backend.connected:
            self._offer_restore()
        if self.host is not None:
            self._announce_to_host()
        # Started last so the loop is already doing real work when it begins
        # sampling, and stopped in action_quit.
        diag.start_watchdog()
        # Git status and PR info are polled by the engine, once a minute and
        # only for its interest set, which this TUI keeps at the selected
        # worktree (see on_list_view_highlighted). The diff is only wanted
        # while it is shown, so this client polls it.
        self.set_interval(
            30, diag.wrap_timer("timer.diff", self._refresh_selected_diff)
        )

    async def on_unmount(self) -> None:
        # Also stopped in action_quit; this covers every other way out so the
        # watchdog thread doesn't outlive the loop it is watching and report a
        # dead loop as a stall.
        diag.event(log, "app.unmount")
        diag.stop_watchdog()
        await self._stop_engine()

    async def _stop_engine(self) -> None:
        if self._unsubscribe_engine is not None:
            self._unsubscribe_engine()
            self._unsubscribe_engine = None
        if self._detach_ui is not None:
            self._detach_ui()
            self._detach_ui = None
        await self.backend.stop()

    def _load_worktrees(self) -> None:
        self.backend.refresh_worktrees()

    # --- Engine events ---

    def _on_engine_event(self, event: EngineEvent) -> None:
        """Apply an engine change to the screen.

        The engine runs on this app's loop, so this is already the main
        thread. Reactions that mount or remove widgets are queued with
        ``call_later``, which runs them one after the other, in event order.
        """
        name, data = event.event, event.data
        if name == WORKTREES_CHANGED:
            self._apply_worktrees(self.worktrees)
        elif name == GIT_STATUS_UPDATED:
            statuses = self._git_statuses
            self._apply_git_statuses({p: statuses[p] for p in data if p in statuses})
        elif name == PR_INFO_UPDATED:
            selected = self._selected_worktree
            if selected is not None and selected.path == data["path"]:
                self._apply_pr_info(self.backend.get_pr_info(data["path"]))
        elif name in (AGENT_STATUS, AGENT_UPDATED):
            self._refresh_sidebar_agents(data["worktree_path"])
        elif name == AGENT_SPAWNED:
            self.call_later(self._show_agent, data["worktree_path"], data["agent_id"], data["kind"])
        elif name == AGENT_EXITED:
            # A remote backend's store forgets the agent on this event, after
            # the agent.status that announced it.
            self._refresh_sidebar_agents(data["worktree_path"])
            self.call_later(self._hide_agent, data)
        elif name == ENGINE_NOTICE:
            self.notify(data["message"], severity=data["severity"], timeout=5)
        elif name in _JOB_EVENTS:
            self.query_one(JobLog).on_job_event(event)
        elif name == UI_COMMAND and self._ui_commands is not None:
            self._ui_commands.put_nowait(data)
        elif name == BACKEND_DISCONNECTED:
            self.sub_title = "engine disconnected"
            self.notify(
                f"Lost the engine ({data['reason']}); reconnecting…",
                severity="error",
                timeout=10,
            )
        elif name == BACKEND_CONNECTED:
            if not data["first"]:
                self.call_later(self._resync, data["engine_changed"])
            if data["first"] or data["engine_changed"]:
                self._offer_restore()
                # An engine slower than the first-connect wait was announced
                # without a socket; a new engine has a new one.
                if self.host is not None:
                    self.call_later(self._announce_to_host)

    async def _show_agent(self, key: str, agent_id: str, kind: str) -> None:
        """Mount the view of an agent the engine has just spawned."""
        session = await self.backend.session(key, agent_id)
        if session is None:
            return  # gone again before its tab could be shown
        center = self.query_one(CenterPanel)
        # The keyboard follows a new agent only into the panel on screen: an
        # agent that shows up elsewhere (spawned over MCP, or already running
        # when this TUI connected) must not take the keys out of sight.
        if kind == ORCHESTRATOR_KIND:
            orchestrator = await center.ensure_orchestrator_panel(self._repo_root)
            await orchestrator.show_agent(session, focus=self._shown is None)
            self._publish_view()
            return
        panel = await center.ensure_panel(key)
        await panel.show_agent(
            session, self.registry.label(key, agent_id), focus=self._shown == key
        )
        self._publish_view()

    async def _hide_agent(self, data: dict) -> None:
        """Remove the view of an agent the engine no longer runs."""
        key = data["worktree_path"]
        panel = self._get_any_panel(key)
        if panel is not None:
            await panel.cleanup_agent(data["agent_id"])
        self._publish_view()
        if data.get("stopped"):
            return
        label = "Orchestrator" if key == ORCHESTRATOR_KEY else "Agent"
        self.notify(
            f"{label} exited — press s to spawn again",
            severity="warning",
            timeout=5,
        )

    async def _resync(self, engine_changed: bool) -> None:
        """Catch up with an engine reached again: drop the tabs of agents it
        no longer runs, show the ones it does, and (for an engine that came
        back as a new process) open the worktree shells again."""
        center = self.query_one(CenterPanel)
        for key, panel in list(center._panels.items()):
            for agent_id in list(panel.agent_ids):
                if not self.backend.has_agent_session(key, agent_id):
                    await panel.cleanup_agent(agent_id)
            if engine_changed and isinstance(panel, WorktreePanel):
                await panel.restart_terminal()
        self._publish_view()
        self.notify("Reconnected to the engine", timeout=3)

    def _apply_worktrees(self, worktrees: list[WorktreeInfo]) -> None:
        wt_list = self.query_one(WorktreeList)
        wt_list.set_worktrees(worktrees, agent_states=self._agent_states)
        # The rebuilt items start without a badge, and the sweep that
        # follows only reports statuses that changed.
        wt_list.update_all_git_statuses(self._git_statuses)

        count = len(worktrees)
        self.sub_title = f"{count} worktree{'s' if count != 1 else ''}"

        # The rebuilt list has no highlight: put it back where it was.
        if self._orchestrator_selected:
            self._queue_selection(None)
        elif self._selected_worktree is not None:
            self._queue_selection(self._selected_worktree.path)

    # Workers get a callable, not a coroutine: a worker cancelled before it
    # starts (exclusive groups, quitting) would leave the coroutine unawaited.

    def _queue_selection(self, path: str | None) -> None:
        """Highlight ``path`` (``None``: the orchestrator) once the sidebar
        is rebuilt. Only the latest request counts, so a create or remove
        that picks its own target overrides keeping the old one."""
        self.run_worker(
            partial(self._select_worktree_by_path, path),
            group="sidebar_selection",
            exclusive=True,
        )

    async def _select_worktree_by_path(self, path: str | None) -> None:
        """Highlight the worktree with the given path in the sidebar list
        (``None``: the orchestrator, the first item).

        Waits for the sidebar rebuild of the last rescan: until its old items
        are gone, an index taken among the children points past the end of
        the rebuilt list.
        """
        wt_list = self.query_one(WorktreeList)
        await wt_list.wait_rebuilt()
        for idx, child in enumerate(wt_list.children):
            if path is None and isinstance(child, OrchestratorListItem):
                wt_list.index = idx
                break
            if isinstance(child, WorktreeListItem) and child.worktree.path == path:
                wt_list.index = idx
                break

    def _get_selected_worktree(self) -> WorktreeInfo | None:
        """Get the currently highlighted worktree."""
        wt_list = self.query_one(WorktreeList)
        if wt_list.highlighted_child is not None and isinstance(
            wt_list.highlighted_child, WorktreeListItem
        ):
            return wt_list.highlighted_child.worktree
        return None

    def _get_agent_state(self, key: str, agent_id: str = "") -> AgentState:
        """Get or create the AgentState for one agent in a worktree/orchestrator."""
        return self.registry.get(key, agent_id)

    def _refresh_sidebar_agents(self, key: str) -> None:
        """Push the worktree's current agent roll-up to the sidebar."""
        self.query_one(WorktreeList).update_agent_states(
            key, self.backend.agent_states(key)
        )

    def _get_any_panel(self, key: str):
        """Get the panel for a worktree path or ORCHESTRATOR_KEY."""
        center = self.query_one(CenterPanel)
        if key == ORCHESTRATOR_KEY:
            return center.get_orchestrator_panel()
        return center.get_panel(key)

    def _refresh_git_statuses(self) -> None:
        """Fetch git status for every worktree, off the message pump.

        Only the full-rescan paths need it; the periodic poll refreshes just
        the selected worktree.
        """
        self.backend.sweep_git_status()

    def _cancel_selection_debounce(self) -> None:
        """Drop refreshes queued for a worktree we have since moved off."""
        if self._selection_debounce is not None:
            self._selection_debounce.stop()
            self._selection_debounce = None

    def _schedule_selection_refresh(self) -> None:
        """Refresh git status, the diff and PR info once the selection settles.

        Debounced rather than immediate: scrolling the sidebar with j/k would
        otherwise launch a ``git status``, a ``git diff`` *and* a ``gh`` call
        for every worktree passed through, and those subprocesses are the
        expensive part — a burst of ``gh`` calls also ties up the thread pool
        the git fetches share. ``exclusive=True`` on the workers cancels
        *waiting* on a superseded fetch but cannot stop a subprocess already
        running, so the throttle has to happen before they are spawned.
        """
        self._cancel_selection_debounce()
        self._selection_debounce = self.set_timer(
            _SELECTION_DEBOUNCE,
            diag.wrap_timer("timer.selection_refresh", self._run_selection_refresh),
        )

    def _run_selection_refresh(self) -> None:
        """Fire the settled-selection refreshes."""
        self._selection_debounce = None
        self._refresh_selected_git_status()
        self._refresh_selected_diff()
        self._refresh_pr_status()

    def _refresh_selected_git_status(self) -> None:
        """Fetch git status for the selected worktree only, off the message pump.

        ``exclusive=True`` drops an in-flight fetch when the selection moves
        or the user asks again — only the current worktree's answer matters.
        """
        wt = self._selected_worktree
        if not self._repo_root or wt is None or wt.is_bare:
            return
        self.run_worker(
            partial(self.backend.refresh_git_status, [wt.path]),
            group="git_status_selected",
            exclusive=True,
        )

    def _apply_git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        """Show changed statuses in the sidebar and the selected panel."""
        self.query_one(WorktreeList).update_all_git_statuses(statuses)
        self._push_git_status_to_selected_panel()

    def _push_git_status_to_selected_panel(self) -> None:
        """Push cached git status to the currently visible panel."""
        wt = self._selected_worktree
        if wt is None:
            return
        gs = self._git_statuses.get(wt.path)
        if gs is None:
            return
        center = self.query_one(CenterPanel)
        panel = center.get_panel(wt.path)
        if panel:
            panel.update_git_status(gs, wt.display_branch)

    def _refresh_selected_diff(self) -> None:
        """Refresh the diff tab for the currently selected worktree.

        `git diff` runs off the message pump. ``exclusive=True`` cancels any
        in-flight refresh when the user moves selection again — we only care
        about the diff for the worktree that's *currently* selected.
        """
        wt = self._selected_worktree
        if wt is None:
            return
        self.run_worker(
            partial(self._fetch_diff, wt.path), group="diff_refresh", exclusive=True
        )

    async def _fetch_diff(self, worktree_path: str) -> None:
        try:
            reply = await self.backend.get_pr_diff(worktree_path)
        except EngineUnavailable:
            return  # the backend reports the outage itself
        self._apply_diff(worktree_path, pr_diff_text(reply))

    def _apply_diff(self, worktree_path: str, diff_text: str) -> None:
        """Apply diff text to the panel — runs on the main thread."""
        if (
            self._selected_worktree is None
            or self._selected_worktree.path != worktree_path
        ):
            return  # selection changed; drop the stale result
        try:
            center = self.query_one(CenterPanel)
        except NoMatches:
            # The fetch outlived the screen: the app is shutting down. The
            # worker would otherwise fail and fail the app's exit with it.
            return
        panel = center.get_panel(worktree_path)
        if panel:
            panel.update_diff(diff_text)

    def _refresh_pr_status(self) -> None:
        """Refresh PR/CI status for the selected worktree now."""
        wt = self._selected_worktree
        if wt is None:
            return
        self.run_worker(partial(self.backend.refresh_pr_info, [wt.path]))

    def _apply_pr_info(self, pr_info) -> None:
        """Apply PR info to the status bar (must run on main thread)."""
        try:
            bar = self.query_one("#pr-status-bar", PrStatusBar)
            bar.update_pr_info(pr_info)
        except Exception:
            pass

    # --- Navigation ---

    async def on_list_view_highlighted(self, event: WorktreeList.Highlighted) -> None:
        center = self.query_one(CenterPanel)
        # Any move invalidates a fetch queued for the worktree we just left.
        self._cancel_selection_debounce()
        with diag.timed(log, "sidebar.highlighted") as span:
            if event.item is not None and isinstance(event.item, OrchestratorListItem):
                span["target"] = "orchestrator"
                self._orchestrator_selected = True
                self._selected_worktree = None
                self.backend.set_interest(())
                with diag.timed(log, "sidebar.switch_to_orchestrator"):
                    await center.switch_to_orchestrator(self._repo_root)
                self._shown = None
            elif event.item is not None and isinstance(event.item, WorktreeListItem):
                span["target"] = event.item.worktree.path
                self._orchestrator_selected = False
                self._selected_worktree = event.item.worktree
                self.backend.set_interest([event.item.worktree.path])
                # Mounting the panel for a worktree visited for the first time
                # also starts its `bash -l` shell, so this is where a "browsing
                # is getting slower" cost would show up.
                with diag.timed(
                    log, "sidebar.switch_to", worktree=event.item.worktree.path
                ):
                    await center.switch_to(event.item.worktree.path)
                self._shown = event.item.worktree.path
                self._push_git_status_to_selected_panel()
                if self.backend.has_pr_info(event.item.worktree.path):
                    self._apply_pr_info(
                        self.backend.get_pr_info(event.item.worktree.path)
                    )
                # Show the cached badge immediately, then refresh the status, the
                # diff and the PR once the selection settles: the periodic poll
                # only covers whichever worktree was selected at the time, so the
                # one we just moved to may be holding stale content.
                self._schedule_selection_refresh()
                span["triggered"] = "selection_refresh(debounced)"
            else:
                span["target"] = None
                self._orchestrator_selected = False
                self._selected_worktree = None
                self.backend.set_interest(())
                self._shown = _NOTHING
        self._publish_view()

    # --- View state (ui.view_changed) ---

    def _view(self) -> dict:
        """What this screen shows, as ``ui.view_changed`` carries it."""
        worktree_path: str | None = None
        agent_id: str | None = None
        center = self.query_one(CenterPanel)
        if self._orchestrator_selected:
            orchestrator = center.get_orchestrator_panel()
            if orchestrator is not None and orchestrator.agent_terminal is not None:
                agent_id = ""
        elif self._selected_worktree is not None:
            worktree_path = self._selected_worktree.path
            panel = center.get_panel(worktree_path)
            if panel is not None:
                agent_id = panel.shown_agent_id
        focus, modal = self._focus_area()
        return {
            "worktree_path": worktree_path,
            "orchestrator": self._orchestrator_selected,
            "agent_id": agent_id,
            "focus": focus,
            "modal": modal,
        }

    def _focus_area(self) -> tuple[str, str | None]:
        """Where the keyboard goes: ``(focus, modal name or None)``."""
        if len(self.screen_stack) > 1:
            screen = self.screen
            name = next(
                (n for cls, n in _MODAL_NAMES if isinstance(screen, cls)),
                type(screen).__name__.lower(),
            )
            return "modal", name
        node = self.focused
        if node is None:
            return "none", None
        # Focus can linger in the panel of a worktree no longer shown.
        center = self.query_one(CenterPanel)
        ancestors = node.ancestors_with_self
        if center in ancestors:
            if self._orchestrator_selected:
                shown = center.get_orchestrator_panel()
            elif self._selected_worktree is not None:
                shown = center.get_panel(self._selected_worktree.path)
            else:
                shown = None
            if shown is None or shown not in ancestors:
                return "none", None
        while node is not None:
            if isinstance(node, MonitoredTerminal):
                return "agent", None
            if isinstance(node, ScrollableTerminal):
                return "shell", None
            if node.id == "diff-scroll":
                return "diff", None
            if isinstance(node, WorktreeList):
                return "sidebar", None
            node = node.parent
        return "none", None

    def _publish_view(self) -> None:
        """Tell the engine (and so every v1 client) what is shown, once it
        has settled for :data:`_VIEW_SETTLE`; a view equal to the last one
        is not announced again."""
        if self._view_hold or self._view_timer is not None:
            return
        self._view_timer = self.set_timer(_VIEW_SETTLE, self._flush_view)

    def _flush_view(self) -> None:
        self._view_timer = None
        if self._view_hold:
            return  # the command announces its outcome when it ends
        try:
            view = self._view()
        except NoMatches:
            return  # torn down, or not composed yet
        self.backend.publish_view(view)

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self._publish_view()

    def push_screen(self, *args, **kwargs):
        # A modal without a focusable widget moves no focus, so the view is
        # published here (once the screen is up) rather than on focus alone.
        result = super().push_screen(*args, **kwargs)
        self.call_after_refresh(self._publish_view)
        return result

    def pop_screen(self):
        result = super().pop_screen()
        self.call_after_refresh(self._publish_view)
        return result

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        self._publish_view()

    # --- Hosted mode ---

    def _announce_to_host(self) -> None:
        """Write the OSC 7717 announcement (socket, engine id) to the host
        terminal, between frames; see :mod:`lazyagent.protocol.hosted`."""
        driver = self._driver
        if driver is None:
            return
        sequence = encode_osc(
            {
                "socket": self.backend.v1_socket_path,
                "engine_id": self.backend.engine_id,
                "repo_root": self._repo_root,
                "pid": os.getpid(),
                "host": self.host,
            }
        )
        if os.environ.get("TMUX"):
            sequence += tmux_passthrough(sequence)
        driver.write(sequence)
        driver.flush()

    def on_resize(self, event: events.Resize) -> None:
        # A host that attached late (a tmux client) gets the announcement on
        # its first resize.
        if self.host is not None and self.backend.v1_socket_path is not None:
            self._announce_to_host()

    async def _run_ui_commands(self) -> None:
        """Carry out the host's ``ui.command`` events, one at a time."""
        assert self._ui_commands is not None
        while True:
            data = await self._ui_commands.get()
            self._view_hold += 1
            try:
                await self._carry_out(data)
                # Widget.focus only queues the move on the app, while the
                # settle timer runs in its own task: a busy app would let it
                # announce the view before the focus lands.
                await self._drain_messages()
            except Exception:
                log.exception("ui.command %r failed", data)
            finally:
                self._view_hold -= 1
            self._publish_view()

    async def _drain_messages(self) -> None:
        """Wait until the app has handled the messages queued so far."""
        drained = asyncio.Event()
        if self.call_later(drained.set):
            await drained.wait()

    async def _carry_out(self, data: dict) -> None:
        command = data.get("command")
        path = data.get("worktree_path")
        if len(self.screen_stack) > 1:
            self.notify(
                "Close the dialog first", severity="warning", timeout=3
            )
            return
        if command == "open_modal" and data.get("modal") == "create":
            self.action_create_worktree()
            return
        if command == "blank":
            await self._blank()
            return
        if command not in (
            "select_worktree",
            "select_orchestrator",
            "focus_agent",
            "open_shell",
            "show_diff",
            "open_modal",
        ):
            log.info("ignoring unknown ui.command %r", command)
            return
        if not await self._show(path):
            return
        if command in ("select_worktree", "select_orchestrator"):
            self._focus_shown()
        elif command == "focus_agent":
            self._focus_agent(data.get("agent_id"))
        elif command == "open_shell":
            self.action_focus_terminal()
        elif command == "show_diff":
            self.action_focus_diff()
            self._refresh_selected_diff()
        elif command == "open_modal":
            modal = data.get("modal")
            if modal == "spawn":
                self.action_spawn_agent()
            elif modal == "remove":
                self.action_remove_worktree()

    async def _blank(self) -> None:
        """Show nothing: the host shows the agents itself, and a panel in
        sight here would keep rendering them for nobody."""
        self._cancel_selection_debounce()
        self.query_one(CenterPanel).show_placeholder(_BLANK_TEXT)
        self.query_one(WorktreeList).index = None
        deadline = time.monotonic() + _UI_SELECT_TIMEOUT
        while self._shown is not _NOTHING and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    async def _show(self, path: str | None) -> bool:
        """Select ``path`` (None: the orchestrator) as the sidebar would and
        wait until its panel is shown."""
        if self._shown != path:
            self._queue_selection(path)
        deadline = time.monotonic() + _UI_SELECT_TIMEOUT
        while self._shown != path:
            if time.monotonic() > deadline:
                log.warning("ui.command: %r was not shown in time", path)
                return False
            await asyncio.sleep(0.02)
        panel = self.query_one(CenterPanel).get_panel(path) if path else None
        if panel is not None:
            # A first visit opens the shell (a round trip to a remote
            # engine): the command's focus lands once its view is there.
            try:
                await asyncio.wait_for(
                    panel.shell_ready.wait(), max(0.0, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                log.warning("ui.command: the shell of %r was not shown in time", path)
        return True

    def _focus_shown(self) -> None:
        """Give the keyboard to what a selection shows: the active agent,
        else the worktree shell. The host's selection is the click it
        stands for."""
        center = self.query_one(CenterPanel)
        if self._orchestrator_selected:
            orchestrator = center.get_orchestrator_panel()
            if orchestrator is not None and orchestrator.agent_terminal is not None:
                orchestrator.agent_terminal.focus()
            return
        wt = self._selected_worktree
        panel = center.get_panel(wt.path) if wt is not None else None
        if panel is None:
            return
        terminal = panel.agent_terminal
        if terminal is not None:
            panel.switch_to_tab(f"agent-tab-{terminal.session.agent_id}")
            terminal.focus()
        else:
            self.action_focus_terminal()

    def _focus_agent(self, agent_id: str | None) -> None:
        if agent_id is None or self._orchestrator_selected:
            self.action_focus_agent()
            return
        wt = self._selected_worktree
        panel = self.query_one(CenterPanel).get_panel(wt.path) if wt else None
        terminal = panel.get_agent(agent_id) if panel is not None else None
        if terminal is None:
            self.notify(f"Agent {agent_id} is not shown", severity="warning")
            return
        panel.switch_to_tab(f"agent-tab-{agent_id}")
        terminal.focus()

    # --- Actions ---

    def action_spawn_agent(self) -> None:
        if self._orchestrator_selected:
            self._spawn_orchestrator_agent()
            return

        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        # Correlates every step from this keypress to the agent's first output.
        spawn_id = diag.next_id("spawn") if diag.ENABLED else ""
        modal_opened = time.perf_counter()

        async def on_spawn_dismiss(result: SpawnResult | None) -> None:
            diag.event(
                log,
                "spawn.modal_dismissed",
                spawn_id=spawn_id,
                accepted=result is not None,
                open_ms=round((time.perf_counter() - modal_opened) * 1000, 2),
            )
            if result is not None and worktree is not None:
                with diag.timed(
                    log, "spawn.total", spawn_id=spawn_id, worktree=worktree.path
                ) as span:
                    center = self.query_one(CenterPanel)
                    # switch_to (not just ensure_panel) so the panel is visible
                    with diag.timed(log, "spawn.switch_to", spawn_id=spawn_id):
                        await center.switch_to(worktree.path)
                    try:
                        info = await self.backend.spawn_agent(
                            worktree.path,
                            result.instruction,
                            skip_permissions=result.skip_permissions,
                            resume_mode=result.resume_mode,
                            env=dict(os.environ),
                            spawn_id=spawn_id,
                        )
                    except (ValueError, EngineUnavailable) as e:
                        self.notify(str(e), severity="error", timeout=5)
                        return
                    span["agent_id"] = info.agent_id

        provider = self.backend.get_config().agent_provider
        with diag.timed(
            log, "spawn.push_modal", spawn_id=spawn_id, worktree=worktree.path
        ):
            self.push_screen(SpawnModal(worktree.display_label, agent_provider=provider), on_spawn_dismiss)

    def _spawn_orchestrator_agent(self) -> None:
        """Spawn agent in the orchestrator panel."""
        if self.backend.has_agent(ORCHESTRATOR_KEY):
            self.notify("Orchestrator agent already running", severity="warning")
            return

        async def on_spawn_dismiss(result: SpawnResult | None) -> None:
            if result is not None:
                center = self.query_one(CenterPanel)
                await center.switch_to_orchestrator(self._repo_root)
                try:
                    await self.backend.spawn_agent(
                        self._repo_root,
                        result.instruction,
                        kind=ORCHESTRATOR_KIND,
                        skip_permissions=result.skip_permissions,
                        resume_mode=result.resume_mode,
                        env=dict(os.environ),
                    )
                except (ValueError, EngineUnavailable) as e:
                    self.notify(str(e), severity="warning")

        provider = self.backend.get_config().agent_provider
        self.push_screen(SpawnModal("Orchestrator", agent_provider=provider), on_spawn_dismiss)

    async def action_stop_agent(self) -> None:
        if self._orchestrator_selected:
            self._stop_orchestrator_agent()
            return

        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        center = self.query_one(CenterPanel)
        panel = center.get_panel(worktree.path)
        agent_id = panel.active_agent_id if panel else None
        if agent_id is None or not self.backend.has_agent_session(worktree.path, agent_id):
            self.notify("No running agent in this worktree", severity="warning")
            return

        # The tab goes when the engine reports the agent gone.
        self.backend.stop_agent(worktree.path, agent_id)
        self.notify("Agent stopped")

    def _stop_orchestrator_agent(self) -> None:
        """Stop the orchestrator agent."""
        if not self.backend.has_agent(ORCHESTRATOR_KEY):
            self.notify("No running orchestrator agent", severity="warning")
            return

        self.backend.stop_agent(ORCHESTRATOR_KEY, "")
        self.notify("Orchestrator agent stopped")

    def action_focus_sidebar(self) -> None:
        if self.host is not None:
            # No sidebar to go to: leave the terminal, so q, s, x, c and d
            # reach the app again.
            self.set_focus(None)
            self._publish_view()
            return
        self.query_one(WorktreeList).focus()

    def action_focus_agent(self) -> None:
        # Ctrl+J. Everything here runs on the message pump, so any duration
        # logged below is time the whole UI was unresponsive.
        with diag.timed(log, "ctrlj.focus_agent") as span:
            if self._orchestrator_selected:
                span["target"] = "orchestrator"
                panel = self.query_one(CenterPanel).get_orchestrator_panel()
                if panel and panel.agent_terminal:
                    span["outcome"] = "focus"
                    panel.agent_terminal.focus()
                else:
                    span["outcome"] = "spawn"
                    self.action_spawn_agent()
                return

            wt = self._get_selected_worktree()
            if not wt:
                span["outcome"] = "no_worktree"
                return
            span["worktree"] = wt.path
            panel = self.query_one(CenterPanel).get_panel(wt.path)
            if panel:
                agent_id = panel.active_agent_id or (panel.agent_ids[-1] if panel.agent_ids else None)
                if agent_id is not None:
                    span["outcome"] = "focus"
                    span["agent_id"] = agent_id
                    with diag.timed(log, "ctrlj.switch_to_tab", agent_id=agent_id):
                        panel.switch_to_tab(f"agent-tab-{agent_id}")
                    terminal = panel.get_agent(agent_id)
                    if terminal:
                        with diag.timed(log, "ctrlj.focus_terminal"):
                            terminal.focus()
                else:
                    span["outcome"] = "spawn"
                    self.action_spawn_agent()
            else:
                span["outcome"] = "no_panel"

    def action_next_agent(self) -> None:
        self._cycle_agent(1)

    def action_prev_agent(self) -> None:
        self._cycle_agent(-1)

    def _cycle_agent(self, delta: int) -> None:
        """Switch focus to the next/previous agent tab, wrapping around."""
        if self._orchestrator_selected:
            return  # orchestrator is single-agent
        wt = self._get_selected_worktree()
        if not wt:
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel is None:
            return
        ids = panel.agent_ids
        if len(ids) < 2:
            return  # nothing to cycle between
        current = panel.active_agent_id
        idx = ids.index(current) if current in ids else 0
        new_id = ids[(idx + delta) % len(ids)]
        panel.switch_to_tab(f"agent-tab-{new_id}")
        terminal = panel.get_agent(new_id)
        if terminal:
            terminal.focus()

    def action_focus_diff(self) -> None:
        wt = self._get_selected_worktree()
        if not wt:
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel:
            panel.switch_to_tab("diff-tab")
            try:
                panel.query_one("#diff-scroll").focus()
            except Exception:
                pass

    def action_focus_terminal(self) -> None:
        wt = self._get_selected_worktree()
        if not wt:
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel:
            panel.focus_shell()

    async def action_quit(self) -> None:
        diag.event(log, "app.quit", worktrees=len(self.worktrees))
        diag.stop_watchdog()
        await self._stop_engine()
        self.exit()

    # --- Restoring agents (engine-split §6e) ---

    def _offer_restore(self) -> None:
        """Say so when the engine (once per engine) has agents an earlier
        session ran: they are not brought back unasked."""
        engine_id = getattr(self.backend, "engine_id", None) or "in-process"
        if engine_id == self._restore_offered_for:
            return
        self._restore_offered_for = engine_id
        self.run_worker(self._offer_restore_now(), group="restore_offer")

    async def _offer_restore_now(self) -> None:
        try:
            agents = await self.backend.list_restorable()
        except (EngineUnavailable, ValueError):
            return
        if agents:
            count = len(agents)
            self.notify(
                f"{count} agent{'s' if count != 1 else ''} from an earlier "
                "session can be brought back: press R (or Restore agents in "
                "the command palette)",
                title="Restore agents",
                timeout=15,
            )

    def action_restore_agents(self) -> None:
        self.run_worker(self._restore_agents(), group="restore", exclusive=True)

    async def _restore_agents(self) -> None:
        try:
            result = await self.backend.restore_agents()
        except (EngineUnavailable, ValueError) as e:
            self.notify(f"Could not restore: {e}", severity="error", timeout=5)
            return
        restored, failed = result["restored"], result["failed"]
        if not restored and not failed:
            self.notify("Nothing to restore", timeout=3)
            return
        if restored:
            count = len(restored)
            self.notify(
                f"Restored {count} agent{'s' if count != 1 else ''}", timeout=5
            )
        for failure in failed:
            self.notify(
                f"Could not restore {failure.get('agent_id') or failure['id']}: "
                f"{failure['reason']}",
                severity="warning",
                timeout=8,
            )

    def get_system_commands(self, screen):  # type: ignore[override]
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Restore agents",
            "Bring back the agents an earlier session ran when it ended",
            self.action_restore_agents,
        )

    def action_refresh(self) -> None:
        self._load_worktrees()
        self.notify("Refreshed worktrees")

    def action_refresh_git_status(self) -> None:
        """Refresh git status for the selected worktree, nothing else."""
        if self._selected_worktree is None:
            return
        # Explicit request — fire now. Any pending selection refresh is left
        # alone: it also refreshes the diff, which this shortcut does not.
        self._refresh_selected_git_status()
        self.notify("Refreshing git status…", timeout=2)

    def action_create_worktree(self) -> None:
        config = self.backend.get_config()

        def on_modal_dismiss(result: CreateWorktreeResult | None) -> None:
            if result is None:
                return
            # A custom create command can run for minutes; its output shows
            # in the job log meanwhile.
            self.run_worker(partial(self._do_create_worktree, result), group="worktree_job")

        self.push_screen(
            CreateWorktreeModal(
                default_branch=default_base(config.default_branch, self.worktrees),
                show_extra_options=config.has_custom_create,
            ),
            on_modal_dismiss,
        )

    async def _do_create_worktree(self, result: CreateWorktreeResult) -> None:
        try:
            created = await self.backend.create_worktree(
                result.branch,
                result.base_branch,
                result.extra_options,
                env=dict(os.environ),
            )
        except (WorktreeManagerError, ValueError, EngineUnavailable) as e:
            self.notify(str(e), severity="error", timeout=5)
            return
        self._queue_selection(created.path)
        self.notify(f"Created worktree: {os.path.basename(created.path)}")

    def action_remove_worktree(self) -> None:
        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return
        if worktree.is_main:
            self.notify("Cannot remove the main worktree", severity="error")
            return

        if self.backend.has_active_agents(worktree.path):
            self.notify(
                "Agent is running in this worktree — stop it first (x)",
                severity="warning",
            )
            return

        def on_remove_dismiss(result: RemoveWorktreeResult | None) -> None:
            if result is not None and worktree is not None:
                self.run_worker(
                    partial(self._do_remove_worktree, worktree, force=result.force),
                    group="worktree_job",
                )

        self.push_screen(
            RemoveWorktreeModal(
                title="Remove worktree",
                body=f"Remove [bold]{worktree.display_label}[/bold] ({worktree.name})?",
            ),
            on_remove_dismiss,
        )

    async def _do_remove_worktree(self, worktree: WorktreeInfo, *, force: bool = False) -> None:
        try:
            await self.backend.remove_worktree(worktree.path, force, env=dict(os.environ))
        except (WorktreeManagerError, ValueError, EngineUnavailable) as e:
            self.notify(str(e), severity="error", timeout=5)
            return
        if self.worktrees:
            self._queue_selection(None)
        self.notify(f"Removed worktree: {worktree.name}")

    def action_open_pr_url(self, url: str) -> None:
        self.open_url(url)

    def action_help(self) -> None:
        self.push_screen(HelpModal(hosted=self.host is not None))


def run_tui(repo_path: str | None = None, *, launch: Any = None) -> None:
    """Run the TUI on the backend ``LAZYAGENT_ENGINE`` asks for; ``launch``
    is an engine the CLI already started (see lazyagent.cli)."""
    log_file = diag.setup()
    if log_file is not None:
        print(f"lazyagent: diagnostic log -> {log_file}", file=sys.stderr)

    try:
        app = LazyAgent(
            repo_path=repo_path, backend=make_backend(repo_path, launch=launch)
        )
        app.run()
    except WorktreeManagerError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """``lazyagent``; kept for callers of the old entry point."""
    from lazyagent.cli import main as cli_main

    cli_main()
