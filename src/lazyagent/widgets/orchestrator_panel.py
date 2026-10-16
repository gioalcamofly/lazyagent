from __future__ import annotations

from textual.containers import Container
from textual.widgets import Static

from lazyagent.agent_providers import (
    agent_session_name,
    DEFAULT_AGENT_PROVIDER,
    ResumeMode,
    get_agent_provider,
)
from lazyagent.engine.registry import ORCHESTRATOR_KEY, AgentRegistry
from lazyagent.engine.session import AgentSession
from lazyagent.widgets.monitored_terminal import MonitoredTerminal

__all__ = ["ORCHESTRATOR_KEY", "OrchestratorPanel"]

_PLACEHOLDER_TEXT = "Press [bold]s[/bold] to spawn orchestrator"


class OrchestratorPanel(Container):
    """Simplified center panel for the orchestrator — just a single MonitoredTerminal."""

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
        self._session_name = ""

    def compose(self):
        yield Static(_PLACEHOLDER_TEXT, id="orch-placeholder")

    @property
    def agent_terminal(self) -> MonitoredTerminal | None:
        return self._agent_terminal

    def get_agent(self, agent_id: str = "") -> MonitoredTerminal | None:
        """The orchestrator is single-agent; ``agent_id`` is ignored."""
        return self._agent_terminal

    def get_session(self, agent_id: str = "") -> AgentSession | None:
        """The orchestrator's session; ``agent_id`` is ignored."""
        terminal = self._agent_terminal
        return terminal.session if terminal is not None else None

    def agent_session_name(self, agent_id: str = "") -> str:
        """The name other Claude sessions address the orchestrator by."""
        return self._session_name

    @property
    def agent_ids(self) -> list[str]:
        return [""] if self._agent_terminal is not None else []

    @property
    def has_agent(self) -> bool:
        return self._agent_terminal is not None and self._agent_terminal.session.running

    async def cleanup_agent(self, agent_id: str | None = None) -> None:
        """Remove the agent terminal and restore the placeholder."""
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            await self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            self.query_one("#orch-placeholder")
        except Exception:
            self.mount(Static(_PLACEHOLDER_TEXT, id="orch-placeholder"))

    async def spawn_agent(
        self,
        skip_permissions: bool = False,
        agent_provider: str = DEFAULT_AGENT_PROVIDER,
        resume_mode: ResumeMode = ResumeMode.NEW,
        socket_path: str | None = None,
        instruction: str | None = None,
        system_prompt: str | None = None,
        cross_session_inbound: str | None = None,
    ) -> None:
        """Spawn the orchestrator agent process."""
        # Remove previous terminal or placeholder
        if self._agent_terminal is not None:
            self._agent_terminal.stop()
            await self._agent_terminal.remove()
            self._agent_terminal = None

        try:
            placeholder = self.query_one("#orch-placeholder", Static)
            await placeholder.remove()
        except Exception:
            pass

        provider = get_agent_provider(agent_provider)
        self._session_name = (
            agent_session_name(self.worktree_path, "orchestrator")
            if provider.session_name_flag
            else ""
        )
        self.registry.register(ORCHESTRATOR_KEY, "", session_name=self._session_name)
        runtime_context = provider.build_runtime_context(
            self.worktree_path,
            socket_path=socket_path,
            cross_session_inbound=cross_session_inbound,
        )
        command = provider.build_command(
            self.worktree_path,
            skip_permissions=skip_permissions,
            runtime_context=runtime_context,
            resume_mode=resume_mode,
            instruction=instruction,
            system_prompt=system_prompt,
            session_name=self._session_name or None,
        )

        session = AgentSession(
            command,
            worktree_path=ORCHESTRATOR_KEY,
            observer=provider.create_observer_from_context(runtime_context),
        )
        terminal = MonitoredTerminal(session, id="orch-agent-terminal")
        self._agent_terminal = terminal
        self.mount(terminal)
        terminal.start()
        terminal.focus()
