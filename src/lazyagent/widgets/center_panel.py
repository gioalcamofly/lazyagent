from __future__ import annotations

import hashlib
import shlex

from rich.text import Text
from textual.containers import Container, VerticalScroll
from textual.widgets import ContentSwitcher, Static, TabbedContent, TabPane

from lazyagent.agent_providers import (
    DEFAULT_AGENT_PROVIDER,
    ResumeMode,
    env_exports,
    get_agent_provider,
)
from lazyagent.models import GitStatus
from lazyagent.styles import SCROLLBAR_CSS
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY, OrchestratorPanel
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal


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

    DEFAULT_CSS = f"""
    WorktreePanel {{
        layout: vertical;
        width: 1fr;
        height: 1fr;
    }}
    #agent-tabs {{
        height: 2fr;
        border: solid $secondary;
        border-title-color: $text-muted;
    }}
    #agent-tabs:focus-within {{
        border: solid $accent;
        border-title-color: $accent;
    }}
    #agent-tab {{
        height: 1fr;
    }}
    #diff-tab {{
        height: 1fr;
    }}
    #diff-scroll {{
        height: 1fr;
        width: 1fr;
        overflow-y: auto;
        overflow-x: hidden;
        background: $background;
{SCROLLBAR_CSS}
    }}
    #diff-content {{
        width: 1fr;
        height: auto;
        padding: 0 1;
    }}
    #terminal-pane {{
        height: 1fr;
        border: solid $secondary;
        border-title-color: $text-muted;
    }}
    #terminal-pane:focus-within {{
        border: solid $accent;
        border-title-color: $accent;
    }}
    #agent-placeholder {{
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }}
    #terminal-placeholder {{
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }}
    ScrollableTerminal {{ height: 1fr; width: 1fr; }}
    """

    def __init__(self, worktree_path: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.worktree_path = worktree_path
        self._agent_terminal: MonitoredTerminal | None = None

    def compose(self):
        yield GitInfoBar(id="git-info-bar")
        with TabbedContent(id="agent-tabs"):
            with TabPane("Agent", id="agent-tab"):
                yield Static(
                    "Press [bold]s[/bold] or [bold]Ctrl+J[/bold] to spawn agent",
                    id="agent-placeholder",
                )
            with TabPane("Diff", id="diff-tab"):
                with VerticalScroll(id="diff-scroll"):
                    yield Static(
                        Text("No changes"),
                        id="diff-content",
                    )
        with Container(id="terminal-pane"):
            yield Static(
                "Terminal",
                id="terminal-placeholder",
            )

    def on_mount(self) -> None:
        terminal_pane = self.query_one("#terminal-pane", Container)
        terminal_pane.border_title = "Ctrl+L Terminal"
        self._try_start_terminal()

    def _try_start_terminal(self) -> None:
        """Try to mount a real terminal widget."""
        try:
            placeholder = self.query_one("#terminal-placeholder", Static)
            pane = self.query_one("#terminal-pane", Container)
            placeholder.remove()
            script = (
                f"{env_exports()}"
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

    def update_diff(self, diff_text: str) -> None:
        """Update the diff tab content."""
        try:
            diff_widget = self.query_one("#diff-content", Static)
            if diff_text:
                diff_widget.update(Text(diff_text))
            else:
                diff_widget.update(Text("No changes"))
        except Exception:
            pass

    def switch_to_tab(self, tab_id: str) -> None:
        """Switch the TabbedContent to the given tab."""
        try:
            tabs = self.query_one("#agent-tabs", TabbedContent)
            tabs.active = tab_id
        except Exception:
            pass

    @property
    def agent_terminal(self) -> MonitoredTerminal | None:
        return self._agent_terminal

    @property
    def has_agent(self) -> bool:
        return (
            self._agent_terminal is not None
            and self._agent_terminal.emulator is not None
        )

    async def cleanup_agent(self) -> None:
        """Remove the agent terminal widget and restore the placeholder."""
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            await self._agent_terminal.remove()
            self._agent_terminal = None

        pane = self.query_one("#agent-tab", TabPane)
        try:
            pane.query_one("#agent-placeholder")
        except Exception:
            pane.mount(
                Static(
                    "Press [bold]s[/bold] or [bold]Ctrl+J[/bold] to spawn agent",
                    id="agent-placeholder",
                )
            )

    async def spawn_agent(
        self,
        skip_permissions: bool = False,
        agent_provider: str = DEFAULT_AGENT_PROVIDER,
        resume_mode: ResumeMode = ResumeMode.NEW,
        socket_path: str | None = None,
        instruction: str | None = None,
    ) -> None:
        """Spawn the configured coding agent process in the Agent pane."""
        pane = self.query_one("#agent-tab", TabPane)

        # Remove previous terminal or placeholder (await to ensure DOM is clean
        # before mounting the new widget with the same ID).
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            await self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            placeholder = self.query_one("#agent-placeholder", Static)
            await placeholder.remove()
        except Exception:
            pass

        provider = get_agent_provider(agent_provider)
        runtime_context = provider.build_runtime_context(
            self.worktree_path, socket_path=socket_path
        )
        command = provider.build_command(
            self.worktree_path,
            skip_permissions=skip_permissions,
            runtime_context=runtime_context,
            resume_mode=resume_mode,
            instruction=instruction,
        )

        terminal = MonitoredTerminal(
            command=command,
            worktree_path=self.worktree_path,
            observer=provider.create_observer_from_context(runtime_context),
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
        # worktree_path -> panel widget. Stored by instance (not DOM id) so we
        # can reserve the slot before ContentSwitcher.add_content's await
        # completes; concurrent ensure_panel calls for the same key then
        # dedupe instead of trying to mount a second widget with the same id.
        self._panels: dict[str, Container] = {}

    def compose(self):
        yield Static(
            "Select a worktree to begin",
            id="center-placeholder",
        )
        yield ContentSwitcher(id="panel-switcher", initial=None)

    def _get_panel_by_key(self, key: str) -> Container | None:
        """Get an existing panel by key, or None."""
        return self._panels.get(key)

    def _activate_panel(self, key: str) -> None:
        """Hide placeholder and switch the ContentSwitcher to the given key."""
        self.query_one("#center-placeholder", Static).display = False
        self.query_one("#panel-switcher", ContentSwitcher).current = self._panels[key].id

    async def ensure_panel(self, worktree_path: str) -> WorktreePanel:
        """Get or lazily create a WorktreePanel for the given worktree.

        Uses ContentSwitcher.add_content (not raw mount) so the new panel is
        hidden until ``_activate_panel`` flips it via ``current``. Raw mount
        leaves ``display=True`` and the panel renders as a stacked sibling.
        """
        existing = self._panels.get(worktree_path)
        if existing is not None:
            return existing  # type: ignore[return-value]

        panel_id = _panel_id(worktree_path)
        panel = WorktreePanel(worktree_path, id=panel_id)
        self._panels[worktree_path] = panel
        switcher = self.query_one("#panel-switcher", ContentSwitcher)
        await switcher.add_content(panel, id=panel_id)
        return panel

    async def switch_to(self, worktree_path: str) -> WorktreePanel:
        """Switch the visible panel to the given worktree (creating if needed)."""
        panel = await self.ensure_panel(worktree_path)
        self._activate_panel(worktree_path)
        return panel

    def get_panel(self, worktree_path: str) -> WorktreePanel | None:
        """Get existing WorktreePanel or None."""
        return self._get_panel_by_key(worktree_path)  # type: ignore[return-value]

    async def ensure_orchestrator_panel(self, repo_root: str) -> OrchestratorPanel:
        """Get or lazily create the OrchestratorPanel."""
        existing = self._panels.get(ORCHESTRATOR_KEY)
        if existing is not None:
            return existing  # type: ignore[return-value]

        panel_id = "wp-orchestrator"
        panel = OrchestratorPanel(worktree_path=repo_root, id=panel_id)
        self._panels[ORCHESTRATOR_KEY] = panel
        switcher = self.query_one("#panel-switcher", ContentSwitcher)
        await switcher.add_content(panel, id=panel_id)
        return panel

    async def switch_to_orchestrator(self, repo_root: str) -> OrchestratorPanel:
        """Switch the visible panel to the orchestrator."""
        panel = await self.ensure_orchestrator_panel(repo_root)
        self._activate_panel(ORCHESTRATOR_KEY)
        return panel

    def get_orchestrator_panel(self) -> OrchestratorPanel | None:
        """Get the orchestrator panel if it exists."""
        return self._get_panel_by_key(ORCHESTRATOR_KEY)  # type: ignore[return-value]
