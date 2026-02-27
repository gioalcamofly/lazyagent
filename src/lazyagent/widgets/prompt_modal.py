from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class SpawnModal(ModalScreen[bool | None]):
    """Minimal terminal-style modal for choosing agent permissions mode."""

    DEFAULT_CSS = """
    SpawnModal {
        align: center middle;
    }

    SpawnModal > Vertical {
        width: 50;
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

    SpawnModal .modal-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("n", "normal", "Normal", show=False),
        Binding("d", "skip", "Skip Permissions", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, worktree_label: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.worktree_label = worktree_label

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
                "[dim]esc to cancel[/dim]",
                classes="modal-hint",
            )

    def action_normal(self) -> None:
        self.dismiss(False)

    def action_skip(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(None)
