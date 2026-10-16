"""An engine with no TUI in front of it: ``python -m lazyagent.engine [repo]``.

It serves the repo on its v1 socket (and the legacy IPC socket the agents'
MCP servers use) until SIGTERM, SIGINT or SIGHUP, then stops, and its
agents and shells with it. TUIs reach it with ``LAZYAGENT_ENGINE=remote``;
quitting one of them leaves the engine and its agents running.

This is the minimal foreground runner. :func:`serve` is the part a
supervising CLI (start/stop/status, lock, detach, logs) builds on.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable

from lazyagent import diagnostics as diag
from lazyagent.engine.engine import Engine
from lazyagent.engine.events import ENGINE_NOTICE, EngineEvent

log = logging.getLogger(__name__)

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


async def serve(
    repo_path: str | None = None,
    *,
    stop: asyncio.Event | None = None,
    on_ready: Callable[[Engine], None] | None = None,
    engine_factory: Callable[[str | None], Engine] = Engine,
) -> int:
    """Run an engine for ``repo_path`` until ``stop`` is set; returns an
    exit status (1 when it cannot serve the repo on a v1 socket, e.g.
    another engine already does).

    ``on_ready`` gets the started engine. Engine notices are logged.
    """
    stop = stop if stop is not None else asyncio.Event()
    engine = engine_factory(repo_path)

    def on_notice(event: EngineEvent) -> None:
        level = (
            logging.INFO if event.data["severity"] == "information" else logging.WARNING
        )
        log.log(level, "%s", event.data["message"])

    unsubscribe = engine.subscribe(on_notice, [ENGINE_NOTICE])
    try:
        await engine.start()
        if engine.v1_socket_path is None:
            log.error(
                "cannot serve %s on a protocol v1 socket", engine.repo_root or repo_path
            )
            return 1
        if on_ready is not None:
            on_ready(engine)
        await stop.wait()
        return 0
    finally:
        unsubscribe()
        await engine.stop()


def run_engine(repo_path: str | None = None) -> int:
    """:func:`serve` in a fresh event loop, stopped by SIGTERM, SIGINT or
    SIGHUP."""

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in _STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)

        def ready(engine: Engine) -> None:
            print(
                f"lazyagent engine: serving {engine.repo_root} on "
                f"{engine.v1_socket_path} (pid {os.getpid()}); "
                "SIGTERM or Ctrl+C stops it and its agents",
                file=sys.stderr,
                flush=True,
            )

        try:
            return await serve(repo_path, stop=stop, on_ready=ready)
        finally:
            for sig in _STOP_SIGNALS:
                loop.remove_signal_handler(sig)

    return asyncio.run(main())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lazyagent.engine",
        description="Run a lazyagent engine with no TUI (connect with "
        "LAZYAGENT_ENGINE=remote lazyagent)",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="Path to git repository (default: auto-detect from cwd)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="lazyagent engine: %(message)s")
    # The engine's own modules log a lot at INFO; only notices and errors
    # belong on the console.
    logging.getLogger("lazyagent").setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    log_file = diag.setup()
    if log_file is not None:
        print(f"lazyagent engine: diagnostic log -> {log_file}", file=sys.stderr)
    return run_engine(args.repo)
