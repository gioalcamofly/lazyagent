from __future__ import annotations

import hashlib
import os
import shlex

from textual.containers import Container
from textual.widgets import ContentSwitcher, Static

from lazyagent.models import GitStatus
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal

_SENTINEL_SYSTEM_PROMPT = (
    "Always output exactly 'your turn' on its own line "
    "when you need user input or have completed your task."
)


def _panel_id(worktree_path: str) -> str:
    """Derive a DOM-safe ID from a worktree path."""
    return "wp-" + hashlib.md5(worktree_path.encode()).hexdigest()[:8]


class GitInfoBar(Static):
    """Thin bar showing git status for the current worktree."""

    DEFAULT_CSS = """
    GitInfoBar {
        height: 1;
        width: 1fr;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=True, **kwargs)

    def update_status(self, git_status: GitStatus, branch: str) -> None:
        """Re-render the bar with new git info."""
        # Truncate branch and commit subject
        b = branch[:30] + "\u2026" if len(branch) > 30 else branch
        subj = git_status.last_commit_subject
        subj = subj[:50] + "\u2026" if len(subj) > 50 else subj

        parts: list[str] = [f"[bold]{b}[/bold]"]
        if subj:
            parts.append(f"[dim]{subj}[/dim]")

        if git_status.dirty_count > 0:
            parts.append(f"[yellow]*{git_status.dirty_count} dirty[/yellow]")
        else:
            parts.append("[green]clean[/green]")

        if git_status.has_upstream:
            if git_status.ahead == 0 and git_status.behind == 0:
                parts.append("[green]in sync[/green]")
            else:
                if git_status.ahead:
                    parts.append(f"[cyan]\u2191{git_status.ahead}[/cyan]")
                if git_status.behind:
                    parts.append(f"[red]\u2193{git_status.behind}[/red]")
        else:
            parts.append("[dim]no upstream[/dim]")

        self.update("  ".join(parts))


class WorktreePanel(Container):
    """Per-worktree panel with Agent and Terminal panes in a vertical split."""

    DEFAULT_CSS = """
    WorktreePanel {
        layout: vertical;
        width: 1fr;
        height: 1fr;
    }
    #agent-pane {
        height: 2fr;
        border: solid $secondary;
        border-title-color: $text-muted;
    }
    #agent-pane:focus-within {
        border: solid $accent;
        border-title-color: $accent;
    }
    #terminal-pane {
        height: 1fr;
        border: solid $secondary;
        border-title-color: $text-muted;
    }
    #terminal-pane:focus-within {
        border: solid $accent;
        border-title-color: $accent;
    }
    #agent-placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    #terminal-placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    ScrollableTerminal { height: 1fr; width: 1fr; }
    """

    def __init__(self, worktree_path: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.worktree_path = worktree_path
        self._agent_terminal: MonitoredTerminal | None = None

    def compose(self):
        yield GitInfoBar(id="git-info-bar")
        with Container(id="agent-pane"):
            yield Static(
                "Press [bold]s[/bold] or [bold]Ctrl+J[/bold] to spawn agent",
                id="agent-placeholder",
            )
        with Container(id="terminal-pane"):
            yield Static(
                "Terminal",
                id="terminal-placeholder",
            )

    def on_mount(self) -> None:
        agent_pane = self.query_one("#agent-pane", Container)
        agent_pane.border_title = "Ctrl+J Agent"
        terminal_pane = self.query_one("#terminal-pane", Container)
        terminal_pane.border_title = "Ctrl+L Terminal"
        self._try_start_terminal()

    def _try_start_terminal(self) -> None:
        """Try to mount a real terminal widget."""
        try:
            placeholder = self.query_one("#terminal-placeholder", Static)
            pane = self.query_one("#terminal-pane", Container)
            placeholder.remove()
            path_val = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            script = (
                f"export PATH={shlex.quote(path_val)}"
                f" && cd {shlex.quote(self.worktree_path)}"
                f" && exec bash -l"
            )
            terminal = ScrollableTerminal(
                command=f"bash -c {shlex.quote(script)}",
                id="terminal-widget",
            )
            pane.mount(terminal)
            terminal.start()
        except Exception:
            pass

    def update_git_status(self, git_status: GitStatus, branch: str) -> None:
        """Update the git info bar for this panel."""
        try:
            bar = self.query_one("#git-info-bar", GitInfoBar)
            bar.update_status(git_status, branch)
        except Exception:
            pass

    @property
    def agent_terminal(self) -> MonitoredTerminal | None:
        return self._agent_terminal

    @property
    def has_agent(self) -> bool:
        return self._agent_terminal is not None

    def cleanup_agent(self) -> None:
        """Remove the agent terminal widget and restore the placeholder."""
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            self._agent_terminal.remove()
            self._agent_terminal = None

        pane = self.query_one("#agent-pane", Container)
        try:
            pane.query_one("#agent-placeholder")
        except Exception:
            pane.mount(
                Static(
                    "Press [bold]s[/bold] or [bold]Ctrl+J[/bold] to spawn agent",
                    id="agent-placeholder",
                )
            )

    def spawn_agent(self, skip_permissions: bool = False) -> None:
        """Spawn a Claude Code CLI process in the Agent pane."""
        pane = self.query_one("#agent-pane", Container)

        # Remove previous terminal or placeholder
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            placeholder = self.query_one("#agent-placeholder", Static)
            placeholder.remove()
        except Exception:
            pass

        # Build the claude command
        parts = ["claude"]
        if skip_permissions:
            parts.append("--dangerously-skip-permissions")
        parts.extend([
            "--append-system-prompt",
            _SENTINEL_SYSTEM_PROMPT,
        ])

        # Build a shell script that restores PATH (textual-terminal strips
        # the environment to only TERM/LC_ALL/HOME, so claude won't be found),
        # cd's into the worktree, and exec's the command.
        inner_cmd = " ".join(shlex.quote(p) for p in parts)
        path_val = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        script = (
            f"export PATH={shlex.quote(path_val)}"
            f" && cd {shlex.quote(self.worktree_path)}"
            f" && exec {inner_cmd}"
        )
        # shlex.quote the whole script so nested single-quotes are handled
        command = f"bash -c {shlex.quote(script)}"

        terminal = MonitoredTerminal(
            command=command,
            worktree_path=self.worktree_path,
            id="agent-terminal",
        )
        self._agent_terminal = terminal
        pane.mount(terminal)
        terminal.start()

        # Focus the terminal so the user can type immediately
        terminal.focus()


class CenterPanel(Container):
    """Container managing a ContentSwitcher of WorktreePanels."""

    DEFAULT_CSS = """
    CenterPanel {
        width: 1fr;
        height: 1fr;
    }
    ContentSwitcher { height: 1fr; }
    #center-placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._panels: dict[str, str] = {}  # worktree_path -> panel DOM id

    def compose(self):
        yield Static(
            "Select a worktree to begin",
            id="center-placeholder",
        )
        yield ContentSwitcher(id="panel-switcher", initial=None)

    def ensure_panel(self, worktree_path: str) -> WorktreePanel:
        """Get or lazily create a WorktreePanel for the given worktree."""
        if worktree_path in self._panels:
            panel_id = self._panels[worktree_path]
            return self.query_one(f"#{panel_id}", WorktreePanel)

        panel_id = _panel_id(worktree_path)
        panel = WorktreePanel(worktree_path, id=panel_id)
        switcher = self.query_one("#panel-switcher", ContentSwitcher)
        switcher.mount(panel)
        self._panels[worktree_path] = panel_id
        return panel

    def switch_to(self, worktree_path: str) -> WorktreePanel:
        """Switch the visible panel to the given worktree (creating if needed)."""
        panel = self.ensure_panel(worktree_path)
        panel_id = self._panels[worktree_path]

        placeholder = self.query_one("#center-placeholder", Static)
        placeholder.display = False

        switcher = self.query_one("#panel-switcher", ContentSwitcher)
        switcher.current = panel_id
        return panel

    def get_panel(self, worktree_path: str) -> WorktreePanel | None:
        """Get existing panel or None."""
        if worktree_path not in self._panels:
            return None
        panel_id = self._panels[worktree_path]
        return self.query_one(f"#{panel_id}", WorktreePanel)
