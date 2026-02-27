from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


_HELP_TEXT = """\
[bold cyan]Navigation[/bold cyan]
  [bold]j / k[/bold]           Move down / up in sidebar
  [bold]Ctrl+K[/bold]          Focus sidebar
  [bold]Ctrl+J[/bold]          Focus agent pane
  [bold]Ctrl+D[/bold]          Diff pane
  [bold]Ctrl+L[/bold]          Focus terminal pane

[bold cyan]Agents[/bold cyan]
  [bold]s[/bold]               Spawn agent in selected worktree
  [bold]x[/bold]               Stop agent in selected worktree

[bold cyan]Worktrees[/bold cyan]
  [bold]c[/bold]               Create new worktree
  [bold]d[/bold]               Remove selected worktree
  [bold]r[/bold]               Refresh worktree list

[bold cyan]Terminal scrollback[/bold cyan]
  [bold]PageUp / PageDown[/bold]  Scroll terminal history
  [bold]Mouse wheel[/bold]        Scroll terminal history

[bold cyan]General[/bold cyan]
  [bold]?[/bold]               Show this help
  [bold]q[/bold]               Quit\
"""


class HelpModal(ModalScreen[None]):
    """Modal showing all keyboard shortcuts."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
    }

    HelpModal > Vertical {
        width: 52;
        height: auto;
        border: solid $secondary;
        background: $surface;
        padding: 1 2;
    }

    HelpModal .modal-title {
        text-style: bold;
        margin-bottom: 1;
    }

    HelpModal .modal-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("question_mark", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Keyboard Shortcuts", classes="modal-title")
            yield Static(_HELP_TEXT)
            yield Static(
                "[bold cyan]Esc[/bold cyan] / [bold cyan]q[/bold cyan] / [bold cyan]?[/bold cyan]  close",
                classes="modal-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)
