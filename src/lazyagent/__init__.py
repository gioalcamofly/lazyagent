"""lazyagent — a Textual TUI for managing coding agents across git worktrees."""

import lazyagent.pyte_patch  # noqa: F401 — must run before any pyte usage

from importlib.metadata import version

__version__ = version("lazyagent")
