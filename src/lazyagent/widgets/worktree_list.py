from __future__ import annotations

from textual.binding import Binding
from textual.widgets import ListItem, ListView, Static

from lazyagent.models import AgentState, AgentStatus, GitStatus, WorktreeInfo


class WorktreeListItem(ListItem):
    """A single worktree entry in the sidebar list."""

    DEFAULT_CSS = """
    WorktreeListItem {
        height: 4;
        padding: 0 1;
        color: $text;
    }
    WorktreeListItem.-highlight {
        background: $boost;
    }
    WorktreeListItem.--main {
        background: $primary-background-darken-2;
        border-left: tall $primary;
    }
    """

    def __init__(self, worktree: WorktreeInfo, agent_state: AgentState | None = None) -> None:
        super().__init__()
        self.worktree = worktree
        self._agent_state = agent_state or AgentState()
        self._git_status: GitStatus | None = None
        if worktree.is_main:
            self.add_class("--main")

    def compose(self):
        yield Static(self._build_label(), markup=True, id="wt-label")

    def _build_label(self) -> str:
        label = self.worktree.display_label
        branch = self.worktree.display_branch
        status = self._status_line()
        git = self._git_status_line()
        return f"[bold]{label}[/bold]\n{branch}\n{status}\n{git}"

    def _status_line(self) -> str:
        state = self._agent_state
        if state.status == AgentStatus.NO_AGENT:
            return "[dim]---[/dim]"
        elif state.status == AgentStatus.RUNNING:
            return "[green]running[/green]"
        elif state.status == AgentStatus.WAITING:
            return "[bold yellow]waiting[/bold yellow]"
        elif state.status == AgentStatus.POSSIBLY_HANGED:
            return "[bold red]hanged?[/bold red]"
        return "[dim]---[/dim]"

    def _git_status_line(self) -> str:
        gs = self._git_status
        if gs is None:
            return "[dim]...[/dim]"
        parts: list[str] = []
        if gs.dirty_count > 0:
            parts.append(f"[yellow]*{gs.dirty_count}[/yellow]")
        else:
            parts.append("[green]clean[/green]")
        if gs.has_upstream:
            if gs.ahead:
                parts.append(f"[cyan]\u2191{gs.ahead}[/cyan]")
            if gs.behind:
                parts.append(f"[red]\u2193{gs.behind}[/red]")
        return " ".join(parts)

    def update_agent_state(self, state: AgentState) -> None:
        """Re-render the label with updated agent state."""
        self._agent_state = state
        try:
            label_widget = self.query_one("#wt-label", Static)
            label_widget.update(self._build_label())
        except Exception:
            pass

    def update_git_status(self, git_status: GitStatus) -> None:
        """Re-render the label with updated git status."""
        self._git_status = git_status
        try:
            label_widget = self.query_one("#wt-label", Static)
            label_widget.update(self._build_label())
        except Exception:
            pass


class WorktreeList(ListView):
    """Sidebar list of git worktrees with j/k navigation."""

    DEFAULT_CSS = """
    WorktreeList {
        height: 1fr;
        border: solid $secondary;
        border-title-color: $text-muted;
    }
    WorktreeList:focus-within {
        border: solid $accent;
        border-title-color: $accent;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def on_mount(self) -> None:
        self.border_title = "Ctrl+K Sidebar"

    def set_worktrees(self, worktrees: list[WorktreeInfo]) -> None:
        """Replace the list contents with the given worktrees."""
        self.clear()
        for wt in worktrees:
            self.append(WorktreeListItem(wt))

    def update_agent_state(self, worktree_path: str, state: AgentState) -> None:
        """Find the item for the given worktree path and update its agent state."""
        for child in self.children:
            if isinstance(child, WorktreeListItem) and child.worktree.path == worktree_path:
                child.update_agent_state(state)
                break

    def update_all_git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        """Bulk update git status on all items."""
        for child in self.children:
            if isinstance(child, WorktreeListItem) and child.worktree.path in statuses:
                child.update_git_status(statuses[child.worktree.path])
