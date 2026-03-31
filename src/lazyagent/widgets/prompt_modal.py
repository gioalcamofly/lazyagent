from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from lazyagent.agent_providers import ResumeMode


@dataclass
class SpawnResult:
    skip_permissions: bool
    resume_mode: ResumeMode = ResumeMode.NEW


_SESSION_LABELS: dict[ResumeMode, str] = {
    ResumeMode.NEW: "[bold]New[/bold]  [dim](r: resume pick, l: resume last)[/dim]",
    ResumeMode.RESUME_PICK: "[bold cyan]Resume (pick from list)[/bold cyan]  [dim](r/l to change, backspace: new)[/dim]",
    ResumeMode.RESUME_LAST: "[bold cyan]Resume last[/bold cyan]  [dim](r/l to change, backspace: new)[/dim]",
}


class SpawnModal(ModalScreen[SpawnResult | None]):
    """Minimal terminal-style modal for choosing agent permissions and session mode."""

    DEFAULT_CSS = """
    SpawnModal {
        align: center middle;
    }

    SpawnModal > Vertical {
        width: 58;
        height: auto;
        border: solid $secondary;
        background: $surface;
        padding: 1 2;
    }

    SpawnModal .modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    SpawnModal .modal-option {
        margin: 0;
        padding: 0;
    }

    SpawnModal .modal-session {
        margin-top: 1;
    }

    SpawnModal .modal-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("n", "normal", "Normal", show=False),
        Binding("d", "skip", "Skip Permissions", show=False),
        Binding("r", "resume_pick", "Resume Pick", show=False),
        Binding("l", "resume_last", "Resume Last", show=False),
        Binding("backspace", "reset_session", "Reset Session", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, worktree_label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.worktree_label = worktree_label
        self._resume_mode = ResumeMode.NEW

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Spawn agent in [bold]{self.worktree_label}[/bold]",
                classes="modal-title",
            )
            yield Static(
                "[bold cyan]n[/bold cyan] Normal",
                classes="modal-option",
            )
            yield Static(
                "[bold yellow]d[/bold yellow] Dangerously skip permissions",
                classes="modal-option",
            )
            yield Static(
                f"Session: {_SESSION_LABELS[self._resume_mode]}",
                id="session-status",
                classes="modal-session",
            )
            yield Static(
                "[dim]esc to cancel[/dim]",
                classes="modal-hint",
            )

    def _update_session_label(self) -> None:
        self.query_one("#session-status", Static).update(
            f"Session: {_SESSION_LABELS[self._resume_mode]}"
        )

    def action_normal(self) -> None:
        self.dismiss(SpawnResult(skip_permissions=False, resume_mode=self._resume_mode))

    def action_skip(self) -> None:
        self.dismiss(SpawnResult(skip_permissions=True, resume_mode=self._resume_mode))

    def action_resume_pick(self) -> None:
        self._resume_mode = ResumeMode.RESUME_PICK
        self._update_session_label()

    def action_resume_last(self) -> None:
        self._resume_mode = ResumeMode.RESUME_LAST
        self._update_session_label()

    def action_reset_session(self) -> None:
        self._resume_mode = ResumeMode.NEW
        self._update_session_label()

    def action_cancel(self) -> None:
        self.dismiss(None)
