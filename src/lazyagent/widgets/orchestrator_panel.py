from __future__ import annotations

from textual.containers import Container
from textual.widgets import Static

from lazyagent.engine.registry import ORCHESTRATOR_KEY, AgentRegistry
from lazyagent.widgets.monitored_terminal import MonitoredTerminal

__all__ = ["ORCHESTRATOR_KEY", "OrchestratorPanel"]

_PLACEHOLDER_TEXT = "Press [bold]s[/bold] to spawn orchestrator"


class OrchestratorPanel(Container):
    """Simplified center panel for the orchestrator — just a single MonitoredTerminal.

    It shows the orchestrator session the engine runs (:meth:`show_agent`);
    it never starts or stops it.
    """

    DEFAULT_CSS = """
    OrchestratorPanel {
        layout: vertical;
        width: 1fr;
        height: 1fr;
    }
    #orch-placeholder {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self, worktree_path: str, registry: AgentRegistry | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.worktree_path = worktree_path
        self.registry = registry if registry is not None else AgentRegistry()
        self._agent_terminal: MonitoredTerminal | None = None

    def compose(self):
        yield Static(_PLACEHOLDER_TEXT, id="orch-placeholder")

    @property
    def agent_terminal(self) -> MonitoredTerminal | None:
        return self._agent_terminal

    def get_agent(self, agent_id: str = "") -> MonitoredTerminal | None:
        """The orchestrator is single-agent; ``agent_id`` is ignored."""
        return self._agent_terminal

    def get_session(self, agent_id: str = ""):
        """The orchestrator's session; ``agent_id`` is ignored."""
        terminal = self._agent_terminal
        return terminal.session if terminal is not None else None

    def agent_session_name(self, agent_id: str = "") -> str:
        """The name other Claude sessions address the orchestrator by."""
        return self.registry.session_name(ORCHESTRATOR_KEY, "")

    @property
    def agent_ids(self) -> list[str]:
        return [""] if self._agent_terminal is not None else []

    @property
    def has_agent(self) -> bool:
        return self._agent_terminal is not None and self._agent_terminal.session.running

    async def cleanup_agent(self, agent_id: str | None = None) -> None:
        """Remove the agent view and restore the placeholder."""
        if self._agent_terminal is not None:
            await self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            self.query_one("#orch-placeholder")
        except Exception:
            self.mount(Static(_PLACEHOLDER_TEXT, id="orch-placeholder"))

    async def show_agent(self, session) -> None:
        """Show the orchestrator session the engine has spawned and focus it."""
        if self._agent_terminal is not None:
            if self._agent_terminal.session is session:
                return
            await self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            placeholder = self.query_one("#orch-placeholder", Static)
            await placeholder.remove()
        except Exception:
            pass

        terminal = MonitoredTerminal(session, id="orch-agent-terminal")
        self._agent_terminal = terminal
        await self.mount(terminal)
        terminal.focus()
