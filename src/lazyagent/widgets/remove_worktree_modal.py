from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


@dataclass
class RemoveWorktreeResult:
    """Result returned when the remove worktree modal is confirmed."""

    force: bool


class RemoveWorktreeModal(ModalScreen[RemoveWorktreeResult | None]):
    """Modal for confirming worktree removal with optional force flag."""

    DEFAULT_CSS = """
    RemoveWorktreeModal {
        align: center middle;
    }

    RemoveWorktreeModal > Vertical {
        width: 50;
        height: auto;
        border: solid $secondary;
        background: $surface;
        padding: 1 2;
    }

    RemoveWorktreeModal .modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    RemoveWorktreeModal .modal-body {
        margin-bottom: 1;
    }

    RemoveWorktreeModal .modal-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Yes", show=False),
        Binding("n", "deny", "No", show=False),
        Binding("escape", "deny", "Cancel", show=False),
        Binding("f", "toggle_force", "Toggle force", show=False),
    ]

    def __init__(self, title: str, body: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title_text = title
        self._body_text = body
        self._force = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._title_text, classes="modal-title")
            yield Static(self._body_text, classes="modal-body")
            yield Static(self._force_label(), id="force-toggle")
            yield Static(
                "[bold cyan]y[/bold cyan] yes  "
                "[bold cyan]n[/bold cyan] no  "
                "[bold cyan]f[/bold cyan] toggle force",
                classes="modal-hint",
            )

    def _force_label(self) -> str:
        marker = "x" if self._force else " "
        return f"\\[{marker}] Force removal"

    def action_confirm(self) -> None:
        self.dismiss(RemoveWorktreeResult(force=self._force))

    def action_deny(self) -> None:
        self.dismiss(None)

    def action_toggle_force(self) -> None:
        self._force = not self._force
        self.query_one("#force-toggle", Static).update(self._force_label())
