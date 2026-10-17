"""An engine with no TUI in front of it: ``python -m lazyagent.engine [repo]``.

It serves the repo on its v1 socket (and the legacy IPC socket the agents'
MCP servers use) until SIGTERM, SIGINT, SIGHUP or a v1 ``shutdown``, then
stops, and its agents and shells with it. TUIs reach it with
``LAZYAGENT_ENGINE=remote``; quitting one of them leaves the engine and its
agents running.

``lazyagent engine start --foreground`` runs it as is. A detached engine
(``--detached``, started by :mod:`lazyagent.engine.launcher`) logs to its
stdout/stderr, which the launcher points at the repo's engine log: its own
logging rotates that file at a cap and moves stdout/stderr along, so crash
tracebacks and ``faulthandler`` dumps (K3) land in the current file too.
``--ready-fd N`` gets ``ready`` once the engine serves.
"""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import logging
import logging.handlers
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path

from lazyagent import __version__
from lazyagent import diagnostics as diag
from lazyagent.engine.engine import Engine
from lazyagent.engine.events import ENGINE_NOTICE, EngineEvent
from lazyagent.engine.launcher import LOG_BACKUPS, LOG_MAX_BYTES, READY

log = logging.getLogger(__name__)

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


async def serve(
    repo_path: str | None = None,
    *,
    stop: asyncio.Event | None = None,
    on_ready: Callable[[Engine], None] | None = None,
    engine_factory: Callable[[str | None], Engine] = Engine,
) -> int:
    """Run an engine for ``repo_path`` until ``stop`` is set or a client
    asks it to ``shutdown``; returns an exit status (1 when it cannot serve
    the repo on a v1 socket, e.g. another engine already does).

    ``on_ready`` gets the started engine. Engine notices are logged.
    """
    stop = stop if stop is not None else asyncio.Event()
    engine = engine_factory(repo_path)
    reason = "stopped"

    def shutdown() -> None:
        nonlocal reason
        reason = "shutdown"
        log.info("shutdown requested by a client")
        stop.set()

    engine.shutdown_handler = shutdown

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
        await engine.stop(reason)


def run_engine(repo_path: str | None = None, *, ready_fd: int | None = None) -> int:
    """:func:`serve` in a fresh event loop, stopped by SIGTERM, SIGINT or
    SIGHUP. ``ready_fd`` gets ``ready`` once the engine serves, then is
    closed (closed unwritten when it never does)."""

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_log_loop_exception)
        for sig in _STOP_SIGNALS:
            loop.add_signal_handler(sig, stop.set)

        def ready(engine: Engine) -> None:
            log.info(
                "serving %s on %s (pid %d); SIGTERM, Ctrl+C or "
                "`lazyagent engine stop` stops it and its agents",
                engine.repo_root,
                engine.v1_socket_path,
                os.getpid(),
            )
            if ready_fd is not None:
                _signal_ready(ready_fd)

        try:
            return await serve(repo_path, stop=stop, on_ready=ready)
        finally:
            for sig in _STOP_SIGNALS:
                loop.remove_signal_handler(sig)

    try:
        return asyncio.run(main())
    finally:
        if ready_fd is not None:
            _close(ready_fd)


def _signal_ready(fd: int) -> None:
    try:
        os.write(fd, READY + b"\n")
    except OSError:
        pass  # whoever started us is gone; nobody is waiting
    _close(fd)


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _log_loop_exception(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    log.error(
        "unhandled error in the event loop: %s",
        context.get("message"),
        exc_info=context.get("exception"),
    )


class _StdioRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotates the engine log and points stdout/stderr at the new file, so
    whatever is written to them (tracebacks, ``faulthandler``) stays inside
    the cap too."""

    def doRollover(self) -> None:  # noqa: N802 (logging's name)
        super().doRollover()
        if self.stream is not None:
            self.stream.flush()
            for fd in (1, 2):
                os.dup2(self.stream.fileno(), fd)


def _setup_detached_logging() -> None:
    """Log to the file stdout/stderr point at (the launcher's engine log),
    capped; record crashes there."""
    target = Path(os.readlink("/proc/self/fd/2"))
    handler: logging.Handler
    if target.is_file():
        handler = _StdioRotatingHandler(
            target, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS
        )
    else:  # not a regular file: nothing to rotate
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    faulthandler.enable()

    def excepthook(kind, value, tb) -> None:
        log.critical("engine crashed", exc_info=(kind, value, tb))

    sys.excepthook = excepthook


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
    parser.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ready-fd", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.detached:
        _setup_detached_logging()
    else:
        logging.basicConfig(level=logging.INFO, format="lazyagent engine: %(message)s")
    # The engine's own modules log a lot at INFO; only notices and errors
    # belong on the console (or in the log).
    logging.getLogger("lazyagent").setLevel(logging.WARNING)
    log.setLevel(logging.INFO)
    log_file = diag.setup()
    if log_file is not None:
        log.info("diagnostic log -> %s", log_file)
    started = time.monotonic()
    if args.detached:
        log.info(
            "engine %s starting for %s (pid %d, python %s)",
            __version__,
            args.repo,
            os.getpid(),
            sys.version.split()[0],
        )
    status = run_engine(args.repo, ready_fd=args.ready_fd)
    if args.detached:
        log.info(
            "engine exiting with status %d after %.0f s",
            status,
            time.monotonic() - started,
        )
    return status
