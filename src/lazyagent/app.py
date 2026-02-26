from __future__ import annotations

import argparse
import os
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header
from textual import work

from lazyagent.config import Config, format_command, load_config
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentState, AgentStatus, GitStatus, WorktreeInfo
from lazyagent.widgets.center_panel import CenterPanel
from lazyagent.widgets.confirm_modal import ConfirmModal
from lazyagent.widgets.help_modal import HelpModal
from lazyagent.widgets.create_worktree_modal import CreateWorktreeModal, CreateWorktreeResult
from lazyagent.widgets.pr_status_bar import PrStatusBar
from lazyagent.widgets.prompt_modal import SpawnModal
from lazyagent.widgets.worktree_list import WorktreeList, WorktreeListItem
from lazyagent.worktree_manager import WorktreeManager, WorktreeManagerError, find_repo_root


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
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, repo_path: str | None = None) -> None:
        super().__init__()
        self.repo_path = repo_path
        self.worktrees: list[WorktreeInfo] = []
        self._agent_states: dict[str, AgentState] = {}
        self._git_statuses: dict[str, GitStatus] = {}
        self._selected_worktree: WorktreeInfo | None = None
        self._config: Config = Config()
        self._repo_root: str = ""
        self._gh_available: bool | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="sidebar"):
            yield WorktreeList()
            yield PrStatusBar(id="pr-status-bar")
        yield CenterPanel()
        yield Footer()

    def on_mount(self) -> None:
        self._load_worktrees()
        self._load_config()
        self.set_interval(60, self._check_hangs)
        self.set_interval(30, self._refresh_git_statuses)
        self.set_interval(30, self._refresh_selected_diff)
        self.set_interval(60, self._refresh_pr_status)

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
        wt_list.set_worktrees(self.worktrees)

        count = len(self.worktrees)
        self.sub_title = f"{count} worktree{'s' if count != 1 else ''}"

        self._refresh_git_statuses()

    def _get_selected_worktree(self) -> WorktreeInfo | None:
        """Get the currently highlighted worktree."""
        wt_list = self.query_one(WorktreeList)
        if wt_list.highlighted_child is not None and isinstance(
            wt_list.highlighted_child, WorktreeListItem
        ):
            return wt_list.highlighted_child.worktree
        return None

    def _get_agent_state(self, worktree_path: str) -> AgentState:
        if worktree_path not in self._agent_states:
            self._agent_states[worktree_path] = AgentState()
        return self._agent_states[worktree_path]

    def _refresh_git_statuses(self) -> None:
        """Fetch git statuses for all worktrees and push to UI."""
        if not self._repo_root or not self.worktrees:
            return
        try:
            manager = WorktreeManager(self._repo_root)
            self._git_statuses = manager.get_all_git_statuses(self.worktrees)
        except WorktreeManagerError:
            return

        self.query_one(WorktreeList).update_all_git_statuses(self._git_statuses)
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
        """Refresh the diff tab for the currently selected worktree."""
        wt = self._selected_worktree
        if wt is None:
            return
        center = self.query_one(CenterPanel)
        panel = center.get_panel(wt.path)
        if panel:
            diff_text = WorktreeManager.get_diff(wt.path)
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

    def on_list_view_highlighted(self, event: WorktreeList.Highlighted) -> None:
        center = self.query_one(CenterPanel)
        if event.item is not None and isinstance(event.item, WorktreeListItem):
            self._selected_worktree = event.item.worktree
            center.switch_to(event.item.worktree.path)
            self._push_git_status_to_selected_panel()
            self._refresh_selected_diff()
            self._refresh_pr_status()
        else:
            self._selected_worktree = None

    # --- Agent message handlers ---

    def on_agent_status_changed(self, event: AgentStatusChanged) -> None:
        state = self._get_agent_state(event.worktree_path)
        state.status = event.status
        if event.status == AgentStatus.RUNNING:
            # Update last_output_time from the terminal
            center = self.query_one(CenterPanel)
            panel = center.get_panel(event.worktree_path)
            if panel and panel.agent_terminal:
                state.last_output_time = panel.agent_terminal.last_output_time
        self.query_one(WorktreeList).update_agent_state(event.worktree_path, state)

    def on_agent_exited(self, event: AgentExited) -> None:
        state = self._get_agent_state(event.worktree_path)
        state.status = AgentStatus.NO_AGENT
        state.last_output_time = None
        self.query_one(WorktreeList).update_agent_state(event.worktree_path, state)
        self.notify(
            "Agent process exited. Check the Agent tab output for details, then press s to retry.",
            severity="warning",
            timeout=5,
        )

    # --- Hang detection ---

    def _check_hangs(self) -> None:
        """Periodic timer callback: check all active agents for hangs."""
        center = self.query_one(CenterPanel)
        for worktree_path, state in self._agent_states.items():
            if state.status == AgentStatus.RUNNING:
                panel = center.get_panel(worktree_path)
                if panel and panel.agent_terminal:
                    panel.agent_terminal.check_hang()

    # --- Actions ---

    def action_spawn_agent(self) -> None:
        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        center = self.query_one(CenterPanel)
        panel = center.get_panel(worktree.path)
        if panel and panel.has_agent:
            self.notify("Agent already running in this worktree", severity="warning")
            return

        def on_spawn_dismiss(result: bool | None) -> None:
            if result is not None and worktree is not None:
                center = self.query_one(CenterPanel)
                # switch_to (not just ensure_panel) so the panel is visible
                panel = center.switch_to(worktree.path)
                panel.spawn_agent(
                    skip_permissions=result,
                    agent_provider=self._config.agent.provider,
                )

        self.push_screen(SpawnModal(worktree.display_label), on_spawn_dismiss)

    def action_stop_agent(self) -> None:
        worktree = self._get_selected_worktree()
        if worktree is None:
            self.notify("No worktree selected", severity="warning")
            return

        center = self.query_one(CenterPanel)
        panel = center.get_panel(worktree.path)
        if panel is None or not panel.has_agent:
            self.notify("No running agent in this worktree", severity="warning")
            return

        # stop() cancels recv before disconnect fires, so update state directly.
        state = self._get_agent_state(worktree.path)
        state.status = AgentStatus.NO_AGENT
        state.last_output_time = None
        self.query_one(WorktreeList).update_agent_state(worktree.path, state)
        panel.cleanup_agent()
        self.notify("Agent stopped")

    def action_focus_sidebar(self) -> None:
        self.query_one(WorktreeList).focus()

    def action_focus_agent(self) -> None:
        wt = self._get_selected_worktree()
        if not wt:
            return
        panel = self.query_one(CenterPanel).get_panel(wt.path)
        if panel:
            panel.switch_to_tab("agent-tab")
            if panel.agent_terminal:
                panel.agent_terminal.focus()
            else:
                self.action_spawn_agent()

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

    def action_refresh(self) -> None:
        self._load_worktrees()
        self.notify("Refreshed worktrees")

    def action_create_worktree(self) -> None:
        def on_modal_dismiss(result: CreateWorktreeResult | None) -> None:
            if result is None:
                return
            self._do_create_worktree(result)

        self.push_screen(
            CreateWorktreeModal(default_branch=self._config.default_branch),
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
            )
            self._send_to_terminal(cmd)
            self.notify("Command sent to terminal — press r to refresh when done", timeout=5)
        else:
            try:
                manager = WorktreeManager(self._repo_root)
                new_path = manager.create(result.branch, result.base_branch)
                self._load_worktrees()
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

        state = self._get_agent_state(worktree.path)
        if state.status in (AgentStatus.RUNNING, AgentStatus.WAITING):
            self.notify(
                "Agent is running in this worktree — stop it first (x)",
                severity="warning",
            )
            return

        def on_confirm(confirmed: bool) -> None:
            if confirmed and worktree is not None:
                self._do_remove_worktree(worktree)

        self.push_screen(
            ConfirmModal(
                title="Remove worktree",
                body=f"Remove [bold]{worktree.display_label}[/bold] ({worktree.name})?",
            ),
            on_confirm,
        )

    def _do_remove_worktree(self, worktree: WorktreeInfo) -> None:
        if self._config.has_custom_remove:
            cmd = format_command(
                self._config.worktree.remove,  # type: ignore[arg-type]
                branch=worktree.branch or "",
                name=worktree.name,
                base="",
                path=worktree.path,
                repo=self._repo_root,
            )
            self._send_to_terminal(f"cd {self._repo_root} && {cmd}")
            self.action_focus_terminal()
            self.notify("Command sent to terminal — press r to refresh when done", timeout=5)
        else:
            try:
                manager = WorktreeManager(self._repo_root)
                manager.remove(worktree.path)
                self._load_worktrees()
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
            panel = self.query_one(CenterPanel).switch_to(wt.path)
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
