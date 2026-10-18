"""The ``lazyagent`` command: the TUI, or a subcommand.

- ``lazyagent [repo] [--embedded | --remote]``: the TUI. ``--remote`` (or
  ``LAZYAGENT_ENGINE=remote``) runs it on the repo's engine process, which
  it starts detached when none runs; quitting leaves the engine and its
  agents running. ``--embedded`` (the default for now) runs the engine
  inside the TUI.
- ``lazyagent engine …``: run or manage the engine (:mod:`lazyagent.engine.cli`).
- ``lazyagent host …``: the TUI in a web page (experimental).
- ``lazyagent mcp``: the MCP server (``python -m lazyagent.mcp_server``).
- ``lazyagent desktop [repo]``: the native desktop app (:mod:`lazyagent.desktop`).

A repo dir literally named like a subcommand must be passed as ``./engine``.
Subcommands never load Textual. A remote TUI starts the engine before it
loads Textual, so the two boot side by side.
"""

from __future__ import annotations

import argparse
import os
import sys

SUBCOMMANDS = ("engine", "host", "mcp", "desktop")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyagent",
        description="TUI for managing coding agents across git worktrees",
        epilog="Subcommands: lazyagent engine {start,stop,status,logs} [repo] "
        "(the engine that owns the agents); lazyagent host [repo] (the TUI in "
        "a web page, experimental); lazyagent desktop [repo] (the native desktop "
        "app); lazyagent mcp (the MCP server). A repo "
        "dir literally named like a subcommand must be passed as ./engine.",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="Path to git repository (default: auto-detect from cwd)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--remote",
        action="store_true",
        help="Run on the repo's engine process, starting it when none runs; "
        "q leaves it and its agents running (LAZYAGENT_ENGINE=remote)",
    )
    mode.add_argument(
        "--embedded",
        action="store_true",
        help="Run the engine inside the TUI; q stops the agents (the default)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["engine"]:
        from lazyagent.engine.cli import main as engine_main

        sys.exit(engine_main(argv[1:]))
    if argv[:1] == ["host"]:
        from lazyagent.host.cli import main as host_main

        host_main(argv[1:])
        return
    if argv[:1] == ["desktop"]:
        from lazyagent.desktop import main as desktop_main

        sys.exit(desktop_main(argv[1:]))
    if argv[:1] == ["mcp"]:
        from lazyagent.mcp_server import mcp

        mcp.run(transport="stdio")
        return
    args = build_parser().parse_args(argv)
    from lazyagent.backend import ENGINE_ENV, autostart_enabled, wants_remote

    if args.remote or args.embedded:
        os.environ[ENGINE_ENV] = "remote" if args.remote else "embedded"
    try:
        remote = wants_remote()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    launch = _prestart(args.repo) if remote and autostart_enabled() else None

    from lazyagent.app import run_tui

    run_tui(args.repo, launch=launch)


def _prestart(repo: str | None):
    """Start the engine now, if none runs, so it boots while the TUI loads;
    the TUI waits for it (and reports a failure) when it connects."""
    from lazyagent.engine import launcher
    from lazyagent.worktree_manager import WorktreeManagerError

    try:
        return launcher.prestart(launcher.resolve_repo_root(repo))
    except (OSError, WorktreeManagerError):
        return None  # the TUI tries again, and says why it cannot
