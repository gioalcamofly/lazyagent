from __future__ import annotations

import argparse
import os
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.timer import Timer
from textual.widgets import Footer, Header
from textual import work

from lazyagent.config import Config, format_command, load_config
from lazyagent.orchestrator_prompt import compose_orchestrator_prompt
from lazyagent.ipc import IpcServer, start_ipc_server
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentState, AgentStatus, GitStatus, WorktreeInfo
from lazyagent.widgets.center_panel import CenterPanel
from lazyagent.widgets.help_modal import HelpModal
from lazyagent.widgets.create_worktree_modal import CreateWorktreeModal, CreateWorktreeResult
from lazyagent.widgets.remove_worktree_modal import RemoveWorktreeModal, RemoveWorktreeResult
from lazyagent.widgets.pr_status_bar import PrStatusBar
from lazyagent.widgets.prompt_modal import SpawnModal, SpawnResult
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY
from lazyagent.widgets.worktree_list import OrchestratorListItem, WorktreeList, WorktreeListItem
from lazyagent.worktree_manager import WorktreeManager, WorktreeManagerError, find_repo_root


# How long the selection has to settle before we fetch its git status.
# Scrolling the sidebar with j/k should not spawn a `git status` per worktree
# passed through — only the one actually landed on.
_SELECTION_DEBOUNCE = 0.4


class LazyAgent(App):
    """Textual TUI for managing coding agents across git worktrees."""

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
    ]

    def __init__(self, repo_path: str | None = None) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.worktrees: list[WorktreeInfo] = []
        # Two-level registry: outer key is a worktree path (or ORCHESTRATOR_KEY),
        # inner maps agent id -> state. The orchestrator (and any single-agent
        # caller) uses agent id "".
        self._agent_states: dict[str, dict[str, AgentState]] = {}
        self._git_statuses: dict[str, GitStatus] = {}
        self._selected_worktree: WorktreeInfo | None = None
        self._orchestrator_selected: bool = False
        self._config: Config = Config()
        self._repo_root: str = ""
        self._gh_available: bool | None = None
        self._ipc_server: IpcServer | None = None
        self._ipc_socket_path: str | None = None
        self._selection_debounce: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar"):
            yield WorktreeList()
            yield PrStatusBar(id="pr-status-bar")
        yield CenterPanel()
        yield Footer()

    async def on_mount(self) -> None:
        self._load_worktrees()
        self._load_config()
        await self._start_ipc_server()
        self.set_interval(60, self._check_hangs)
        # Only the selected worktree is polled, and only once a minute:
        # `git status` walks the whole working tree, so sweeping every
        # worktree on a timer costs seconds on a repo with many of them.
        # The other worktrees refresh on the full rescan (`r`, create,
        # remove, MCP), and Alt+G refreshes the selected one on demand.
        self.set_interval(60, self._refresh_selected_git_status)
        self.set_interval(30, self._refresh_selected_diff)
        self.set_interval(60, self._refresh_pr_status)

    async def _start_ipc_server(self) -> None:
        """Start the IPC server for MCP communication."""
        try:
            self._ipc_server, self._ipc_socket_path = await start_ipc_server(self)
        except Exception as e:
            self.notify(f"IPC server failed to start: {e}", severity="warning", timeout=5)

    def _load_config(self) -> None:
        if self._repo_root:
            self._config = load_config(self._repo_root)
        else:
            self._config = Config()

    def _load_worktrees(self) -> None:
        try:
            if self.repo_path:
                root = WorktreeManager(self.repo_path).repo_path
            else:
                root = find_repo_root()
            self._repo_root = str(root)
            manager = WorktreeManager(root)
            self.worktrees = manager.list()
        except WorktreeManagerError as e:
            self.notify(str(e), severity="error", timeout=5)
            return

        wt_list = self.query_one(WorktreeList)
        wt_list.set_worktrees(self.worktrees, agent_states=self._agent_states)

        count = len(self.worktrees)
        self.sub_title = f"{count} worktree{'s' if count != 1 else ''}"

        self._refresh_git_statuses()

    def _select_worktree_by_path(self, path: str) -> None:
        """Highlight the worktree with the given path in the sidebar list."""
        wt_list = self.query_one(WorktreeList)
        for idx, child in enumerate(wt_list.children):
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
        bucket = self._agent_states.setdefault(key, {})
        if agent_id not in bucket:
            bucket[agent_id] = AgentState(agent_id=agent_id)
        return bucket[agent_id]

    def _worktree_agent_states(self, key: str) -> dict[str, AgentState]:
        """All agent states for a worktree path / ORCHESTRATOR_KEY (may be empty)."""
        return self._agent_states.get(key, {})

    def _drop_agent_state(self, key: str, agent_id: str) -> None:
        """Remove a single agent from the registry, pruning empty buckets."""
        bucket = self._agent_states.get(key)
        if bucket is not None:
            bucket.pop(agent_id, None)
            if not bucket:
                self._agent_states.pop(key, None)

    def _refresh_sidebar_agents(self, key: str) -> None:
        """Push the worktree's current agent roll-up to the sidebar."""
        self.query_one(WorktreeList).update_agent_states(
            key, self._worktree_agent_states(key)
        )

    def _get_any_panel(self, key: str):
        """Get the panel for a worktree path or ORCHESTRATOR_KEY."""
        center = self.query_one(CenterPanel)
        if key == ORCHESTRATOR_KEY:
            return center.get_orchestrator_panel()
        return center.get_panel(key)

    @work(thread=True, exclusive=True, group="git_status_all")
    def _refresh_git_statuses(self) -> None:
        """Fetch git status for every worktree (runs in a thread).

        ``git status`` walks the whole working tree — on a repo with a couple
        of dozen worktrees this sweep takes seconds, so it must never run on
        the message pump. Only the full-rescan paths need it; the periodic
        poll refreshes just the selected worktree.
        """
        repo_root = self._repo_root
        worktrees = list(self.worktrees)
        if not repo_root or not worktrees:
            return
        try:
            manager = WorktreeManager(repo_root)
            statuses = manager.get_all_git_statuses(worktrees)
        except WorktreeManagerError:
            return
        self.call_from_thread(self._apply_git_statuses, statuses)

    def _cancel_selection_debounce(self) -> None:
        """Drop refreshes queued for a worktree we have since moved off."""
        if self._selection_debounce is not None:
            self._selection_debounce.stop()
            self._selection_debounce = None

    def _schedule_selection_refresh(self) -> None:
        """Refresh git status and the diff once the selection settles.

        Debounced rather than immediate: scrolling the sidebar with j/k would
        otherwise launch a ``git status`` *and* a ``git diff`` for every
        worktree passed through, and those subprocesses are the expensive
        part. ``exclusive=True`` on the workers cancels *waiting* on a
        superseded fetch but cannot stop a subprocess already running, so the
        throttle has to happen before they are spawned.
        """
        self._cancel_selection_debounce()
        self._selection_debounce = self.set_timer(
            _SELECTION_DEBOUNCE, self._run_selection_refresh
        )

    def _run_selection_refresh(self) -> None:
        """Fire the settled-selection refreshes."""
        self._selection_debounce = None
        self._refresh_selected_git_status()
        self._refresh_selected_diff()

    @work(thread=True, exclusive=True, group="git_status_selected")
    def _refresh_selected_git_status(self) -> None:
        """Fetch git status for the selected worktree only (runs in a thread).

        ``exclusive=True`` drops an in-flight fetch when the selection moves
        or the user asks again — only the current worktree's answer matters.
        """
        repo_root = self._repo_root
        wt = self._selected_worktree
        if not repo_root or wt is None or wt.is_bare:
            return
        try:
            manager = WorktreeManager(repo_root)
            status = manager.get_git_status(wt.path)
            status.last_commit_subject = manager.get_last_commit_subject(wt.path)
        except WorktreeManagerError:
            return
        self.call_from_thread(self._apply_git_statuses, {wt.path: status})

    def _apply_git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        """Merge fetched statuses into the cache and UI — runs on the main thread.

        Merges rather than replaces: a selected-worktree refresh must not drop
        the statuses the last full sweep collected for the others.
        """
        self._git_statuses.update(statuses)
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

    @work(thread=True, exclusive=True, group="diff_refresh")
    def _refresh_selected_diff(self) -> None:
        """Refresh the diff tab for the currently selected worktree.

        Runs `git diff` in a worker thread so it doesn't block the message
        pump on every navigation. ``exclusive=True`` cancels any in-flight
        refresh when the user moves selection again — we only care about
        the diff for the worktree that's *currently* selected.
        """
        wt = self._selected_worktree
        if wt is None:
            return
        diff_text = WorktreeManager.get_diff(wt.path)
        self.call_from_thread(self._apply_diff, wt.path, diff_text)

    def _apply_diff(self, worktree_path: str, diff_text: str) -> None:
        """Apply diff text to the panel — runs on the main thread."""
        if (
            self._selected_worktree is None
            or self._selected_worktree.path != worktree_path
        ):
            return  # selection changed; drop the stale result
        panel = self.query_one(CenterPanel).get_panel(worktree_path)
        if panel:
            panel.update_diff(diff_text)

    @work(thread=True)
    def _refresh_pr_status(self) -> None:
        """Refresh PR/CI status for the selected worktree (runs in thread)."""
        wt = self._selected_worktree
        if wt is None:
            return

        if self._gh_available is None:
            self._gh_available = WorktreeManager.is_gh_available()
        if not self._gh_available:
            return

        pr_info = WorktreeManager.get_pr_info(wt.path)
        self.call_from_thread(self._apply_pr_info, pr_info)

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
        if event.item is not None and isinstance(event.item, OrchestratorListItem):
            self._orchestrator_selected = True
            self._selected_worktree = None
            await center.switch_to_orchestrator(self._repo_root)
        elif event.item is not None and isinstance(event.item, WorktreeListItem):
            self._orchestrator_selected = False
            self._selected_worktree = event.item.worktree
            await center.switch_to(event.item.worktree.path)
            self._push_git_status_to_selected_panel()
            # Show the cached badge immediately, then refresh the status and
            # the diff once the selection settles: the periodic poll only
            # covers whichever worktree was selected at the time, so the one
            # we just moved to may be holding stale content.
            self._schedule_selection_refresh()
            self._refresh_pr_status()
        else:
            self._orchestrator_selected = False
            self._selected_worktree = None

    # --- Agent message handlers ---

    def on_agent_status_changed(self, event: AgentStatusChanged) -> None:
        state = self._get_agent_state(event.worktree_path, event.agent_id)
        state.status = event.status
        state.confidence = event.confidence
        state.detail = event.detail
        panel = self._get_any_panel(event.worktree_path)
        if panel is not None and not state.label:
            label_fn = getattr(panel, "agent_label", None)
            if label_fn is not None:
                state.label = label_fn(event.agent_id)
        if event.status == AgentStatus.RUNNING and panel is not None:
            terminal = panel.get_agent(event.agent_id)
            if terminal:
                state.last_output_time = terminal.last_output_time
        self._refresh_sidebar_agents(event.worktree_path)

    async def on_agent_exited(self, event: AgentExited) -> None:
        self._drop_agent_state(event.worktree_path, event.agent_id)
        self._refresh_sidebar_agents(event.worktree_path)

        panel = self._get_any_panel(event.worktree_path)
        if panel is not None:
            await panel.cleanup_agent(event.agent_id)

        label = "Orchestrator" if event.worktree_path == ORCHESTRATOR_KEY else "Agent"
        self.notify(
            f"{label} exited — press s to spawn again",
            severity="warning",
            timeout=5,
        )

    # --- Hang detection ---

    def _check_hangs(self) -> None:
        """Periodic timer callback: check all active agents for hangs."""
        for key, bucket in self._agent_states.items():
            panel = None
            for agent_id, state in bucket.items():
                if state.status != AgentStatus.RUNNING:
                    continue
                if panel is None:
                    panel = self._get_any_panel(key)
                if panel is None:
                    break
                terminal = panel.get_agent(agent_id)
                if terminal:
                    terminal.check_hang()

    # --- Actions ---

    def action_spawn_agent(self) -> None:
        if self._orchestrator_selected:
            self._spawn_orchestrator_agent()
            return

        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        async def on_spawn_dismiss(result: SpawnResult | None) -> None:
            if result is not None and worktree is not None:
                center = self.query_one(CenterPanel)
                # switch_to (not just ensure_panel) so the panel is visible
                panel = await center.switch_to(worktree.path)
                agent_id = await panel.spawn_agent(
                    skip_permissions=result.skip_permissions,
                    agent_provider=self._config.agent.provider,
                    resume_mode=result.resume_mode,
                    socket_path=self._ipc_socket_path,
                    instruction=result.instruction,
                )
                state = self._get_agent_state(worktree.path, agent_id)
                state.label = panel.agent_label(agent_id)
                self._refresh_sidebar_agents(worktree.path)

        self.push_screen(SpawnModal(worktree.display_label, agent_provider=self._config.agent.provider), on_spawn_dismiss)

    def _spawn_orchestrator_agent(self) -> None:
        """Spawn agent in the orchestrator panel."""
        panel = self._get_any_panel(ORCHESTRATOR_KEY)
        if panel and panel.has_agent:
            self.notify("Orchestrator agent already running", severity="warning")
            return

        orch_prompt = compose_orchestrator_prompt(self._config, self._repo_root)

        async def on_spawn_dismiss(result: SpawnResult | None) -> None:
            if result is not None:
                center = self.query_one(CenterPanel)
                panel = await center.switch_to_orchestrator(self._repo_root)
                await panel.spawn_agent(
                    skip_permissions=result.skip_permissions,
                    agent_provider=self._config.agent.provider,
                    resume_mode=result.resume_mode,
                    socket_path=self._ipc_socket_path,
                    instruction=result.instruction,
                    system_prompt=orch_prompt,
                )

        self.push_screen(SpawnModal("Orchestrator", agent_provider=self._config.agent.provider), on_spawn_dismiss)

    async def action_stop_agent(self) -> None:
        if self._orchestrator_selected:
            await self._stop_orchestrator_agent()
            return

        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        center = self.query_one(CenterPanel)
        panel = center.get_panel(worktree.path)
        agent_id = panel.active_agent_id if panel else None
        if panel is None or agent_id is None:
            self.notify("No running agent in this worktree", severity="warning")
            return

        # stop() cancels recv before disconnect fires, so update state directly.
        self._drop_agent_state(worktree.path, agent_id)
        self._refresh_sidebar_agents(worktree.path)
        await panel.cleanup_agent(agent_id)
        self.notify("Agent stopped")

    async def _stop_orchestrator_agent(self) -> None:
        """Stop the orchestrator agent."""
        panel = self._get_any_panel(ORCHESTRATOR_KEY)
        if panel is None or not panel.has_agent:
            self.notify("No running orchestrator agent", severity="warning")
            return

        self._drop_agent_state(ORCHESTRATOR_KEY, "")
        self._refresh_sidebar_agents(ORCHESTRATOR_KEY)
        await panel.cleanup_agent()
        self.notify("Orchestrator agent stopped")

    def action_focus_sidebar(self) -> None:
        self.query_one(WorktreeList).focus()

    def action_focus_agent(self) -> None:
        if self._orchestrator_selected:
            panel = self.query_one(CenterPanel).get_orchestrator_panel()
            if panel and panel.agent_terminal:
                panel.agent_terminal.focus()
            else:
                self.action_spawn_agent()
            return

        wt = self._get_selected_worktree()
        if not wt:
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel:
            agent_id = panel.active_agent_id or (panel.agent_ids[-1] if panel.agent_ids else None)
            if agent_id is not None:
                panel.switch_to_tab(f"agent-tab-{agent_id}")
                terminal = panel.get_agent(agent_id)
                if terminal:
                    terminal.focus()
            else:
                self.action_spawn_agent()

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
            try:
                panel.query_one("#terminal-widget").focus()
            except Exception:
                pass

    async def action_quit(self) -> None:
        if self._ipc_server is not None:
            await self._ipc_server.stop()
            self._ipc_server = None
        self.exit()

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
        def on_modal_dismiss(result: CreateWorktreeResult | None) -> None:
            if result is None:
                return
            self._do_create_worktree(result)

        self.push_screen(
            CreateWorktreeModal(
                default_branch=self._config.default_branch,
                show_extra_options=self._config.has_custom_create,
            ),
            on_modal_dismiss,
        )

    def _do_create_worktree(self, result: CreateWorktreeResult) -> None:
        if self._config.has_custom_create:
            repo_name = os.path.basename(self._repo_root) if self._repo_root else ""
            wt_name = f"{repo_name}-{result.branch}" if repo_name else result.branch
            wt_path = str(
                (self._repo_root and os.path.join(os.path.dirname(self._repo_root), wt_name))
                or wt_name
            )
            cmd = format_command(
                self._config.worktree.create,  # type: ignore[arg-type]
                branch=result.branch,
                name=wt_name,
                base=result.base_branch,
                path=wt_path,
                repo=self._repo_root,
                extra=result.extra_options,
            )
            self._send_to_terminal(cmd)
            self.notify("Command sent to terminal — press r to refresh when done", timeout=5)
        else:
            try:
                manager = WorktreeManager(self._repo_root)
                new_path = manager.create(result.branch, result.base_branch)
                self._load_worktrees()
                self._select_worktree_by_path(new_path)
                self.notify(f"Created worktree: {os.path.basename(new_path)}")
            except WorktreeManagerError as e:
                self.notify(str(e), severity="error", timeout=5)

    def action_remove_worktree(self) -> None:
        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return
        if worktree.is_main:
            self.notify("Cannot remove the main worktree", severity="error")
            return

        states = self._worktree_agent_states(worktree.path)
        if any(
            s.status in (AgentStatus.RUNNING, AgentStatus.WAITING)
            for s in states.values()
        ):
            self.notify(
                "Agent is running in this worktree — stop it first (x)",
                severity="warning",
            )
            return

        def on_remove_dismiss(result: RemoveWorktreeResult | None) -> None:
            if result is not None and worktree is not None:
                self._do_remove_worktree(worktree, force=result.force)

        self.push_screen(
            RemoveWorktreeModal(
                title="Remove worktree",
                body=f"Remove [bold]{worktree.display_label}[/bold] ({worktree.name})?",
            ),
            on_remove_dismiss,
        )

    def _do_remove_worktree(self, worktree: WorktreeInfo, *, force: bool = False) -> None:
        force_flag = "--force" if force else ""
        if self._config.has_custom_remove:
            cmd = format_command(
                self._config.worktree.remove,  # type: ignore[arg-type]
                branch=worktree.branch or "",
                name=worktree.name,
                base="",
                path=worktree.path,
                repo=self._repo_root,
                force=force_flag,
            )
            self._send_to_terminal(f"cd {self._repo_root} && {cmd}")
            self.action_focus_terminal()
            self.notify("Command sent to terminal — press r to refresh when done", timeout=5)
        else:
            try:
                manager = WorktreeManager(self._repo_root)
                manager.remove(worktree.path, force=force)
                self._agent_states.pop(worktree.path, None)
                self._load_worktrees()
                if self.worktrees:
                    self.query_one(WorktreeList).index = 0
                self.notify(f"Removed worktree: {worktree.name}")
            except WorktreeManagerError as e:
                self.notify(str(e), severity="error", timeout=5)

    def _send_to_terminal(self, cmd: str) -> None:
        """Send a command string to the active worktree's terminal pane."""
        wt = self._get_selected_worktree()
        if not wt:
            self.notify(f"No terminal available. Run manually:\n{cmd}", severity="warning", timeout=8)
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel is None:
            self.notify(f"No terminal available. Run manually:\n{cmd}", severity="warning", timeout=8)
            return
        try:
            terminal = panel.query_one("#terminal-widget")
            # send_queue is an asyncio.Queue — must use put_nowait from sync context
            terminal.send_queue.put_nowait(["stdin", cmd + "\n"])
        except Exception:
            self.notify(f"No terminal available. Run manually:\n{cmd}", severity="warning", timeout=8)

    def action_open_pr_url(self, url: str) -> None:
        self.open_url(url)

    def action_help(self) -> None:
        self.push_screen(HelpModal())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lazyagent",
        description="TUI for managing coding agents across git worktrees",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="Path to git repository (default: auto-detect from cwd)",
    )
    args = parser.parse_args()

    try:
        app = LazyAgent(repo_path=args.repo)
        app.run()
    except WorktreeManagerError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
