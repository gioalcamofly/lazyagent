from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Container, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import ContentSwitcher, Static, TabbedContent, TabPane

from lazyagent import diagnostics as diag
from lazyagent.engine.registry import AgentRegistry
from lazyagent.models import GitStatus
from lazyagent.styles import SCROLLBAR_CSS
from lazyagent.widgets.job_log import JobLog
from lazyagent.widgets.monitored_terminal import MonitoredTerminal
from lazyagent.widgets.orchestrator_panel import ORCHESTRATOR_KEY, OrchestratorPanel
from lazyagent.widgets.scrollable_terminal import ScrollableTerminal

if TYPE_CHECKING:
    from lazyagent.backend import Backend

log = logging.getLogger(__name__)


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


def base_label(base: dict) -> str:
    """Where a ``get_diff`` (``pr``) base came from, for its heading."""
    source = base.get("source")
    if source == "pr":
        number = base.get("pr_number")
        return f"PR #{number} base" if number else "PR base"
    if source == "default":
        return "default branch"
    return "override" if source == "override" else str(source or "")


def pr_diff_text(reply: dict) -> str:
    """The Diff tab's text for a ``get_diff`` reply with ``pr``: what the
    pull request shows, then what is not committed yet, under its own
    heading (agents mostly work uncommitted)."""
    base = reply.get("base")
    if base:
        head = f"Against {base['ref']} ({base_label(base)}) · merge-base {base['merge_base'][:10]}"
        committed = reply.get("diff") or "No committed changes"
    else:
        head = f"No base to diff against: {reply.get('base_error') or 'unknown'}"
        committed = ""
    parts = [head]
    if reply.get("fetch_error"):
        parts.append(f"Fetch failed: {reply['fetch_error']}")
    if committed:
        parts.append(committed)
    parts.append("── Uncommitted (staged, unstaged, untracked) ──")
    parts.append(reply.get("uncommitted") or "Nothing uncommitted")
    return "\n\n".join(parts)


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
    """Per-worktree panel with Agent and Terminal panes in a vertical split.

    It shows sessions the engine runs and never starts or stops one itself:
    the shell comes from ``backend.open_shell`` and each agent tab is added
    by :meth:`show_agent` when the engine reports a spawn.
    """

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

    def __init__(
        self,
        worktree_path: str,
        registry: AgentRegistry | None = None,
        backend: Backend | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.worktree_path = worktree_path
        self.backend = backend
        # Holds each agent's label and session name.
        if registry is None:
            registry = backend.registry if backend is not None else AgentRegistry()
        self.registry = registry
        # agent_id -> terminal. Ordered by spawn order; each agent gets its
        # own TabPane to the left of the permanent Diff tab.
        self._agents: dict[str, MonitoredTerminal] = {}
        # Set once the shell's view is mounted (or could not be): opening it
        # is a round trip to a remote engine.
        self.shell_ready = asyncio.Event()
        # The shell was asked for the keyboard before its view was mounted.
        self._focus_shell = False

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
        """Show the worktree's shell, which the engine starts on first use."""
        if self.backend is None:
            self.shell_ready.set()
            return
        self.call_later(self._start_terminal)

    async def _start_terminal(self) -> None:
        backend = self.backend
        if backend is None:
            return
        # Every worktree ever visited leaves one of these shells alive, so this
        # is traced per worktree to make that population visible in the log.
        with diag.timed(log, "panel.start_shell", worktree=self.worktree_path):
            try:
                # This client's environment, not the engine's.
                session = await backend.open_shell(
                    self.worktree_path, env=dict(os.environ)
                )
                pane = self.query_one("#terminal-pane", Container)
                for old in pane.query("#terminal-placeholder, #terminal-widget"):
                    await old.remove()
                terminal = ScrollableTerminal(session, id="terminal-widget")
                await pane.mount(terminal)
                if self._focus_shell:
                    self._focus_shell = False
                    terminal.focus()
            except Exception:
                log.exception("could not open the shell of %s", self.worktree_path)
            finally:
                self.shell_ready.set()

    def focus_shell(self) -> None:
        """Give the keyboard to the shell, now or once its view is mounted."""
        try:
            self.query_one("#terminal-widget").focus()
        except NoMatches:
            self._focus_shell = not self.shell_ready.is_set()

    async def restart_terminal(self) -> None:
        """Show the shell again after its session went away (an engine that
        came back is a new engine, with new shells)."""
        await self._start_terminal()

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

    def get_session(self, agent_id: str):
        """Return the session behind a specific agent, or None."""
        terminal = self._agents.get(agent_id)
        return terminal.session if terminal is not None else None

    def agent_label(self, agent_id: str) -> str:
        """Return the human-facing tab label for an agent."""
        return self.registry.label(self.worktree_path, agent_id)

    def agent_session_name(self, agent_id: str) -> str:
        """Return the name other Claude sessions address this agent by."""
        return self.registry.session_name(self.worktree_path, agent_id)

    @property
    def shown_agent_id(self) -> str | None:
        """The agent whose tab is active, or None (Diff or placeholder)."""
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
        return None

    @property
    def active_agent_id(self) -> str | None:
        """The agent whose tab is currently active, the sole agent, or None.

        Falls back to the only agent when the active tab is Diff/placeholder
        but exactly one agent exists, so ``x``/focus behave intuitively.
        """
        aid = self.shown_agent_id
        if aid is not None:
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
        return any(t.session.running for t in self._agents.values())

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

        Only the view goes: the engine stops the session. ``agent_id=None``
        removes every tab.
        """
        tabs = self.query_one("#agent-tabs", TabbedContent)
        targets = (
            list(self._agents.keys())
            if agent_id is None
            else [agent_id] if agent_id in self._agents else []
        )
        for aid in targets:
            self._agents.pop(aid, None)
            try:
                await tabs.remove_pane(_agent_tab_id(aid))
            except Exception:
                pass

        if not self._agents:
            await self._ensure_placeholder_tab()

    async def show_agent(self, session, label: str, *, focus: bool = True) -> None:
        """Add a tab for an agent the engine has spawned and make it active;
        focus it unless ``focus`` is false (its panel is not shown)."""
        agent_id = session.agent_id
        if agent_id in self._agents:
            return
        tabs = self.query_one("#agent-tabs", TabbedContent)

        # First agent: drop the "press s to spawn" placeholder tab.
        try:
            await tabs.remove_pane(_PLACEHOLDER_TAB_ID)
        except Exception:
            pass

        with diag.timed(
            log, "spawn.mount_pane", spawn_id=session.spawn_id, agent_id=agent_id
        ):
            terminal = MonitoredTerminal(session, id=_agent_terminal_id(agent_id))
            self._agents[agent_id] = terminal
            pane = TabPane(label, terminal, id=_agent_tab_id(agent_id))
            await tabs.add_pane(pane, before=_DIFF_TAB_ID)

        # Make the new agent's tab active and focus its terminal.
        with diag.timed(
            log, "spawn.activate_tab", spawn_id=session.spawn_id, agent_id=agent_id
        ):
            tabs.active = _agent_tab_id(agent_id)
            if focus:
                # Widget.focus() is deferred: by the time it runs a newer
                # agent's tab may be up, and focusing this terminal would
                # switch back to its tab and hide that one.
                self.app.call_later(self._focus_if_shown, agent_id)

    def _focus_if_shown(self, agent_id: str) -> None:
        terminal = self._agents.get(agent_id)
        if terminal is not None and self.shown_agent_id == agent_id:
            terminal.focus()


class CenterPanel(Container):
    """Container managing a ContentSwitcher of WorktreePanels."""

    DEFAULT_CSS = """
    CenterPanel {
        width: 1fr;
        height: 1fr;
    }
    #panel-switcher { height: 1fr; }
    #center-placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        registry: AgentRegistry | None = None,
        backend: Backend | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.backend = backend
        if registry is None:
            registry = backend.registry if backend is not None else AgentRegistry()
        self.registry = registry
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
        yield JobLog()

    def _get_panel_by_key(self, key: str) -> Container | None:
        """Get an existing panel by key, or None."""
        return self._panels.get(key)

    def _activate_panel(self, key: str) -> None:
        """Hide placeholder and switch the ContentSwitcher to the given key."""
        self.query_one("#center-placeholder", Static).display = False
        self.query_one("#panel-switcher", ContentSwitcher).current = self._panels[key].id

    def show_placeholder(self, text: str) -> None:
        """Show ``text`` instead of any panel: every panel is hidden, so
        their terminal views stop rendering (and detach) until the next
        switch."""
        placeholder = self.query_one("#center-placeholder", Static)
        placeholder.update(text)
        placeholder.display = True
        self.query_one("#panel-switcher", ContentSwitcher).current = None

    async def ensure_panel(self, worktree_path: str) -> WorktreePanel:
        """Get or lazily create a WorktreePanel for the given worktree.

        Uses ContentSwitcher.add_content (not raw mount) so the new panel is
        hidden until ``_activate_panel`` flips it via ``current``. Raw mount
        leaves ``display=True`` and the panel renders as a stacked sibling.
        """
        existing = self._panels.get(worktree_path)
        if existing is not None:
            return existing  # type: ignore[return-value]

        # First visit to this worktree: mounting the panel also starts its
        # shell. `panels` says how many are alive by now.
        with diag.timed(log, "panel.create", worktree=worktree_path) as span:
            panel_id = _panel_id(worktree_path)
            panel = WorktreePanel(
                worktree_path, self.registry, backend=self.backend, id=panel_id
            )
            self._panels[worktree_path] = panel
            switcher = self.query_one("#panel-switcher", ContentSwitcher)
            await switcher.add_content(panel, id=panel_id)
            span["panels"] = len(self._panels)
        return panel

    async def switch_to(self, worktree_path: str) -> WorktreePanel:
        """Switch the visible panel to the given worktree (creating if needed)."""
        panel = await self.ensure_panel(worktree_path)
        # Flipping the ContentSwitcher re-lays-out the center pane; `panels` is
        # here to show whether that cost grows with the number of live panels.
        with diag.timed(
            log,
            "panel.activate",
            worktree=worktree_path,
            panels=len(self._panels),
        ):
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
        panel = OrchestratorPanel(
            worktree_path=repo_root, registry=self.registry, id=panel_id
        )
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
