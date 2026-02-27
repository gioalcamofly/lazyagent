from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static


@dataclass
class CreateWorktreeResult:
    """Result returned when the create worktree modal is confirmed."""

    branch: str
    base_branch: str


class CreateWorktreeModal(ModalScreen[CreateWorktreeResult | None]):
    """Modal for entering branch name and base branch to create a worktree."""

    DEFAULT_CSS = """
    CreateWorktreeModal {
        align: center middle;
    }

    CreateWorktreeModal > Vertical {
        width: 50;
        height: auto;
        border: solid $secondary;
        background: $surface;
        padding: 1 2;
    }

    CreateWorktreeModal .modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    CreateWorktreeModal .modal-label {
        margin-top: 1;
        margin-bottom: 0;
    }

    CreateWorktreeModal .modal-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, default_branch: str = "master", **kwargs) -> None:
        super().__init__(**kwargs)
        self._default_branch = default_branch

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Create worktree", classes="modal-title")
            yield Static("Branch name:", classes="modal-label")
            yield Input(placeholder="my-feature-branch", id="branch-input")
            yield Static("Base branch:", classes="modal-label")
            yield Input(value=self._default_branch, id="base-input")
            yield Static("[dim]enter to confirm · esc to cancel[/dim]", classes="modal-hint")

    def on_mount(self) -> None:
        self.query_one("#branch-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "branch-input":
            self.query_one("#base-input", Input).focus()
        elif event.input.id == "base-input":
            self._confirm()

    def _confirm(self) -> None:
        branch = self.query_one("#branch-input", Input).value.strip()
        if not branch:
            self.notify("Branch name is required", severity="warning")
            self.query_one("#branch-input", Input).focus()
            return
        base = self.query_one("#base-input", Input).value.strip() or self._default_branch
        self.dismiss(CreateWorktreeResult(branch=branch, base_branch=base))

    def action_cancel(self) -> None:
        self.dismiss(None)
