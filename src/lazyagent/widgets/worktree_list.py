from __future__ import annotations

from textual.binding import Binding
from textual.widgets import ListItem, ListView, Static

from lazyagent.models import AgentState, AgentStatus, GitStatus, LifecycleConfidence, WorktreeInfo
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY


def format_agent_status(state: AgentState) -> str:
    """Format an AgentState into a Rich-markup status string."""
    status = state.status
    conf = state.confidence

    def _fmt(text: str, color: str) -> str:
        if conf == LifecycleConfidence.LOW:
            return f"[dim {color}]{text}[/dim {color}]"
        return f"[{color}]{text}[/{color}]"

    if status == AgentStatus.NO_AGENT:
        return "[dim]---[/dim]"

    res = ""
    if status == AgentStatus.RUNNING:
        res = _fmt("running", "green")
    elif status in (AgentStatus.WAITING, AgentStatus.WAITING_FOR_USER):
        res = _fmt("waiting", "bold yellow")
    elif status == AgentStatus.WAITING_FOR_APPROVAL:
        res = _fmt("approving", "bold yellow")
    elif status == AgentStatus.COMPLETED:
        res = _fmt("completed", "bold cyan")
    elif status == AgentStatus.FAILED:
        res = _fmt("failed", "bold red")
    elif status == AgentStatus.INTERRUPTED:
        res = _fmt("interrupted", "dim red")
    elif status == AgentStatus.POSSIBLY_HANGED:
        res = "[bold red]hanged?[/bold red]"

    if state.detail and status not in (AgentStatus.RUNNING, AgentStatus.NO_AGENT):
        res += f" [dim]({state.detail})[/dim]"

    return res or "[dim]---[/dim]"


class OrchestratorListItem(ListItem):
    """Special first entry in the sidebar for the orchestrator agent."""

    DEFAULT_CSS = """
    OrchestratorListItem {
        height: 2;
        padding: 0 1;
        color: $text;
        background: $primary-background-darken-1;
        border-left: tall $warning;
    }
    OrchestratorListItem.-highlight {
        background: $boost;
    }
    """

    def __init__(self, agent_state: AgentState | None = None) -> None:
        super().__init__()
        self._agent_state = agent_state or AgentState()

    def compose(self):
        yield Static(self._build_label(), markup=True, id="orch-label")

    def _build_label(self) -> str:
        return f"[bold]ORCH[/bold]\n{format_agent_status(self._agent_state)}"

    def update_agent_state(self, state: AgentState) -> None:
        """Re-render the label with updated agent state."""
        self._agent_state = state
        try:
            label_widget = self.query_one("#orch-label", Static)
            label_widget.update(self._build_label())
        except Exception:
            pass


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
        status = format_agent_status(self._agent_state)
        git = self._git_status_line()
        return f"[bold]{label}[/bold]\n{branch}\n{status}\n{git}"

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

    def set_worktrees(self, worktrees: list[WorktreeInfo], agent_states: dict[str, AgentState] | None = None) -> None:
        """Replace the list contents with the given worktrees, restoring agent state if provided."""
        self.clear()
        agent_states = agent_states or {}
        self.append(OrchestratorListItem(agent_state=agent_states.get(ORCHESTRATOR_KEY)))
        for wt in worktrees:
            state = agent_states.get(wt.path)
            self.append(WorktreeListItem(wt, agent_state=state))

    def update_agent_state(self, key: str, state: AgentState) -> None:
        """Update the sidebar item for the given key (worktree path or ORCHESTRATOR_KEY)."""
        if key == ORCHESTRATOR_KEY:
            for child in self.children:
                if isinstance(child, OrchestratorListItem):
                    child.update_agent_state(state)
                    break
        else:
            for child in self.children:
                if isinstance(child, WorktreeListItem) and child.worktree.path == key:
                    child.update_agent_state(state)
                    break

    def update_all_git_statuses(self, statuses: dict[str, GitStatus]) -> None:
        """Bulk update git status on all items."""
        for child in self.children:
            if isinstance(child, WorktreeListItem) and child.worktree.path in statuses:
                child.update_git_status(statuses[child.worktree.path])
