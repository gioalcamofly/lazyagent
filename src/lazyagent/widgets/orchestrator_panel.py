from __future__ import annotations

from textual.containers import Container
from textual.widgets import Static

from lazyagent.agent_providers import (
    DEFAULT_AGENT_PROVIDER,
    ResumeMode,
    get_agent_provider,
)
from lazyagent.widgets.monitored_terminal import MonitoredTerminal

ORCHESTRATOR_KEY = "__orchestrator__"
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

    def __init__(self, worktree_path: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.worktree_path = worktree_path
        self._agent_terminal: MonitoredTerminal | None = None

    def compose(self):
        yield Static(_PLACEHOLDER_TEXT, id="orch-placeholder")

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
        runtime_context = provider.build_runtime_context(
            self.worktree_path, socket_path=socket_path
        )
        command = provider.build_command(
            self.worktree_path,
            skip_permissions=skip_permissions,
            runtime_context=runtime_context,
            resume_mode=resume_mode,
            instruction=instruction,
            system_prompt=system_prompt,
        )

        terminal = MonitoredTerminal(
            command=command,
            worktree_path=ORCHESTRATOR_KEY,
            observer=provider.create_observer_from_context(runtime_context),
            id="orch-agent-terminal",
        )
        self._agent_terminal = terminal
        self.mount(terminal)
        terminal.start()
        terminal.focus()
