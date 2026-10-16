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
  [bold]s[/bold]               Spawn a new agent (adds a tab; multiple per worktree)
  [bold]x[/bold]               Stop the active agent tab
  [bold]Ctrl+J[/bold]          Focus the active agent tab
  [bold]Alt+] / Alt+n[/bold]    Next agent tab
  [bold]Alt+[ / Alt+p[/bold]    Previous agent tab

[bold cyan]Worktrees[/bold cyan]
  [bold]c[/bold]               Create new worktree
  [bold]d[/bold]               Remove selected worktree
  [bold]r[/bold]               Refresh worktree list (and every git status)
  [bold]Alt+G[/bold]           Refresh git status of the selected worktree

[bold cyan]Terminal scrollback[/bold cyan]
  [bold]PageUp / PageDown[/bold]  Scroll terminal history
  [bold]Mouse wheel[/bold]        Scroll terminal history

[bold cyan]General[/bold cyan]
  [bold]?[/bold]               Show this help
  [bold]q[/bold]               Quit\
"""


# Hosted mode has no sidebar: Ctrl+K leaves the terminal instead.
_SIDEBAR_LINES = """\
  [bold]j / k[/bold]           Move down / up in sidebar
  [bold]Ctrl+K[/bold]          Focus sidebar
"""
_HOSTED_LINES = """\
  [bold]Ctrl+K[/bold]          Leave terminal (keys go to lazyagent)
"""


def help_text(*, hosted: bool = False) -> str:
    """The shortcut list; ``hosted`` describes the hosted-mode keys."""
    return _HELP_TEXT.replace(_SIDEBAR_LINES, _HOSTED_LINES) if hosted else _HELP_TEXT


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

    def __init__(self, *, hosted: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hosted = hosted

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Keyboard Shortcuts", classes="modal-title")
            yield Static(help_text(hosted=self.hosted), id="help-text")
            yield Static(
                "[bold cyan]Esc[/bold cyan] / [bold cyan]q[/bold cyan] / [bold cyan]?[/bold cyan]  close",
                classes="modal-hint",
            )

    def action_close(self) -> None:
        self.dismiss(None)
