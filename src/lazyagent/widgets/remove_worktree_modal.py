from __future__ import annotations

from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from lazyagent.widgets.confirm_modal import ConfirmModal


@dataclass
class RemoveWorktreeResult:
    """Result returned when the remove worktree modal is confirmed."""

    force: bool


class RemoveWorktreeModal(ConfirmModal):
    """Modal for confirming worktree removal with optional force flag."""

    BINDINGS = [
        Binding("f", "toggle_force", "Toggle force", show=False),
    ]

    def __init__(self, title: str, body: str, **kwargs) -> None:
        super().__init__(title=title, body=body, **kwargs)
        self._force = False

    def _extra_compose(self) -> ComposeResult:
        yield Static(self._force_label(), id="force-toggle")

    def _hint_text(self) -> str:
        return (
            "[bold cyan]y[/bold cyan] yes  "
            "[bold cyan]n[/bold cyan] no  "
            "[bold cyan]f[/bold cyan] toggle force"
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
