"""lazyagent — a Textual TUI for managing coding agents across git worktrees."""

try:
    # Written by the build (hatch's version hook): importlib.metadata would
    # cost most of this package's import, in every CLI and MCP process.
    from lazyagent._version import __version__
except ImportError:  # a source tree no build has run in
    from importlib.metadata import version

    __version__ = version("lazyagent")
