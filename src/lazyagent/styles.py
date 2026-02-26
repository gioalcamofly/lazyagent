"""Shared CSS fragments for consistent styling across widgets.

Import ``SCROLLBAR_CSS`` and embed it in any widget's ``DEFAULT_CSS``
to get the same thin, muted scrollbar used by the terminal panes.
"""

# Thin 1-column scrollbar with muted colours that blends into the background.
# Used by ScrollableTerminal, the diff pane, and any future scrollable widgets.
SCROLLBAR_CSS = """\
    scrollbar-size-vertical: 1;
    scrollbar-color: $text-muted 40%;
    scrollbar-color-hover: $text-muted 70%;
    scrollbar-color-active: $text-muted;
    scrollbar-background: $background;
    scrollbar-background-hover: $background;
    scrollbar-background-active: $background;
"""
