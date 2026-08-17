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


_SPAWN_HINT = "Press [bold]s[/bold] or [bold]Ctrl+J[/bold] to spawn agent"
_PLACEHOLDER_TAB_ID = "agent-placeholder-tab"
_DIFF_TAB_ID = "diff-tab"

# Hard ceiling on what the diff Static holds. See update_diff.
_MAX_DIFF_CHARS = 64 * 1024
_MAX_DIFF_LINES = 2000
# A single enormous line (a minified file, a one-line JSON) wraps into
# thousands of visual rows, and it is the wrapped count that costs.
_MAX_DIFF_LINE_CHARS = 1000


def _cap_diff(diff_text: str) -> str:
    """Trim a diff to something a Static can measure cheaply."""
    truncated = len(diff_text) > _MAX_DIFF_CHARS
    lines = diff_text[:_MAX_DIFF_CHARS].split("\n")
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES]
        truncated = True
    capped = []
    for line in lines:
        if len(line) > _MAX_DIFF_LINE_CHARS:
            capped.append(line[:_MAX_DIFF_LINE_CHARS] + " …")
            truncated = True
        else:
            capped.append(line)
    if truncated:
        capped.append("… diff truncated (too large to display)")
    return "\n".join(capped)


def _agent_tab_id(agent_id: str) -> str:
    return f"agent-tab-{agent_id}"


def _agent_terminal_id(agent_id: str) -> str:
    return f"agent-terminal-{agent_id}"


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
    #agent-tabs TabPane {{
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
        # agent_id -> terminal. Ordered by spawn order; each agent gets its
        # own TabPane to the left of the permanent Diff tab.
        self._agents: dict[str, MonitoredTerminal] = {}
        self._labels: dict[str, str] = {}
        self._agent_counter = 0

    def compose(self):
        yield GitInfoBar(id="git-info-bar")
        with TabbedContent(id="agent-tabs"):
            with TabPane("Agent", id=_PLACEHOLDER_TAB_ID):
                yield Static(_SPAWN_HINT, id="agent-placeholder")
            with TabPane("Diff", id=_DIFF_TAB_ID):
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
        """Update the diff tab content.

        The text is capped before it reaches the ``Static``. Textual measures
        a Static's content height by word-wrapping all of it, on every layout
        pass — around 220 ms per megabyte — so an oversized diff does not just
        render slowly, it makes every later mount and resize slow too.
        ``WorktreeManager.get_diff`` already caps its output; this is the
        backstop for anything that reaches the widget by another route.
        """
        try:
            diff_widget = self.query_one("#diff-content", Static)
            if diff_text:
                diff_widget.update(Text(_cap_diff(diff_text)))
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

    # ------------------------------------------------------------------
    # Multi-agent accessors
    # ------------------------------------------------------------------

    @property
    def agent_ids(self) -> list[str]:
        """Agent ids in spawn order."""
        return list(self._agents.keys())

    def get_agent(self, agent_id: str) -> MonitoredTerminal | None:
        """Return the terminal for a specific agent, or None."""
        return self._agents.get(agent_id)

    def agent_label(self, agent_id: str) -> str:
        """Return the human-facing tab label for an agent."""
        return self._labels.get(agent_id, "")

    @property
    def active_agent_id(self) -> str | None:
        """The agent whose tab is currently active, the sole agent, or None.

        Falls back to the only agent when the active tab is Diff/placeholder
        but exactly one agent exists, so ``x``/focus behave intuitively.
        """
        try:
            tabs = self.query_one("#agent-tabs", TabbedContent)
            active = tabs.active or ""
        except Exception:
            active = ""
        prefix = _agent_tab_id("")
        if active.startswith(prefix):
            aid = active[len(prefix):]
            if aid in self._agents:
                return aid
        if len(self._agents) == 1:
            return next(iter(self._agents))
        return None

    @property
    def agent_terminal(self) -> MonitoredTerminal | None:
        """Back-compat: the active agent's terminal (or the sole agent)."""
        aid = self.active_agent_id
        return self._agents.get(aid) if aid else None

    @property
    def has_agent(self) -> bool:
        """True if any spawned agent has a live pty."""
        return any(t.emulator is not None for t in self._agents.values())

    def _new_agent_id(self) -> str:
        self._agent_counter += 1
        return f"a{self._agent_counter}"

    async def _ensure_placeholder_tab(self) -> None:
        """Re-add the placeholder tab when no agents remain."""
        tabs = self.query_one("#agent-tabs", TabbedContent)
        try:
            self.query_one(f"#{_PLACEHOLDER_TAB_ID}", TabPane)
            return  # already present
        except Exception:
            pass
        pane = TabPane(
            "Agent",
            Static(_SPAWN_HINT, id="agent-placeholder"),
            id=_PLACEHOLDER_TAB_ID,
        )
        await tabs.add_pane(pane, before=_DIFF_TAB_ID)
        tabs.active = _PLACEHOLDER_TAB_ID

    async def cleanup_agent(self, agent_id: str | None = None) -> None:
        """Remove one agent's tab (or all), restoring the placeholder if empty.

        ``agent_id=None`` cleans up every agent — used on worktree teardown.
        """
        tabs = self.query_one("#agent-tabs", TabbedContent)
        targets = (
            list(self._agents.keys())
            if agent_id is None
            else [agent_id] if agent_id in self._agents else []
        )
        for aid in targets:
            terminal = self._agents.pop(aid, None)
            self._labels.pop(aid, None)
            if terminal is not None:
                terminal.stop()
            try:
                await tabs.remove_pane(_agent_tab_id(aid))
            except Exception:
                pass

        if not self._agents:
            await self._ensure_placeholder_tab()

    async def spawn_agent(
        self,
        skip_permissions: bool = False,
        agent_provider: str = DEFAULT_AGENT_PROVIDER,
        resume_mode: ResumeMode = ResumeMode.NEW,
        socket_path: str | None = None,
        instruction: str | None = None,
        label: str | None = None,
    ) -> str:
        """Spawn a new coding agent in its own tab. Returns the new agent id."""
        tabs = self.query_one("#agent-tabs", TabbedContent)

        # First agent: drop the "press s to spawn" placeholder tab.
        try:
            await tabs.remove_pane(_PLACEHOLDER_TAB_ID)
        except Exception:
            pass

        agent_id = self._new_agent_id()
        tab_label = label or f"Agent {agent_id[1:]}"
        self._labels[agent_id] = tab_label

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
            agent_id=agent_id,
            id=_agent_terminal_id(agent_id),
        )
        self._agents[agent_id] = terminal
        pane = TabPane(tab_label, terminal, id=_agent_tab_id(agent_id))
        await tabs.add_pane(pane, before=_DIFF_TAB_ID)
        terminal.start()

        # Make the new agent's tab active and focus its terminal.
        tabs.active = _agent_tab_id(agent_id)
        terminal.focus()
        return agent_id


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
