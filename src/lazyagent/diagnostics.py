"""Opt-in diagnostic logging.

Off by default, and as close to free as possible when off: every call site
either tests one of the module-level booleans below first, or calls a helper
whose first statement is that test. Nothing formats a message — or even builds
the argument tuple — unless logging is on.

Enable it with::

    LAZYAGENT_LOG=1              # lifecycle, timers, workers, subprocesses,
                                 # the spawn path, and the event-loop watchdog
    LAZYAGENT_LOG_LEVEL=debug    # ...plus the per-chunk terminal traces

Output is JSON lines — one object per event — written to
``$XDG_STATE_HOME/lazyagent/lazyagent.log`` (``~/.local/state/lazyagent/`` by
default), rotated at 5 MB with 3 backups so a long session is capped at 20 MB
while still keeping the previous run's tail. ``LAZYAGENT_LOG_FILE`` overrides
the path.

Other knobs:

    LAZYAGENT_LOG_WATCHDOG_MS=100   # main-thread block threshold
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

# Checked at every instrumented call site. Read them as ``diagnostics.ENABLED``
# (module attribute) rather than importing the name, so ``setup()`` can flip
# them after import.
ENABLED = False
#: ENABLED *and* the level is DEBUG or finer. Guards the hot paths — per-chunk
#: terminal feed/render traces — which are far too noisy for a normal run.
HOT = False

_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_STACK_DEPTH = 14
_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "", "none"}
_LEVELS = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_configured = False
_log_path: Path | None = None
_seq = 0
_seq_lock = threading.Lock()

log = logging.getLogger("lazyagent.diag")


# ----------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------


def _env_level() -> int | None:
    """The configured level, or None if the environment doesn't ask for logging."""
    raw_level = os.environ.get("LAZYAGENT_LOG_LEVEL", "").strip().lower()
    if raw_level in _LEVELS:
        return _LEVELS[raw_level]

    raw = os.environ.get("LAZYAGENT_LOG")
    if raw is not None:
        value = raw.strip().lower()
        if value in _LEVELS:
            return _LEVELS[value]
        if value in _TRUTHY:
            return logging.INFO
        if value in _FALSY:
            return None
    # A bare LAZYAGENT_LOG_FILE is taken as "log to here", so you can point one
    # run at a scratch file without also remembering the enable flag.
    if os.environ.get("LAZYAGENT_LOG_FILE"):
        return logging.INFO
    return None


def _resolve_path() -> Path:
    override = os.environ.get("LAZYAGENT_LOG_FILE")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME") or str(
        Path.home() / ".local" / "state"
    )
    return Path(state_home).expanduser() / "lazyagent" / "lazyagent.log"


class JsonLineFormatter(logging.Formatter):
    """One JSON object per line, with a millisecond timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            # Plain logging calls from elsewhere in the codebase land under one
            # event name with their text in `msg`, so aggregating by `event`
            # doesn't drown in free-form strings.
            "event": getattr(record, "lz_event", None) or "message",
        }
        fields = getattr(record, "lz_fields", None)
        if fields:
            payload.update(fields)
        if payload["event"] == "message":
            payload["msg"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup() -> Path | None:
    """Configure logging from the environment. Idempotent; no-op when disabled.

    Returns the log file path, or None if logging is off (or the file could not
    be opened — a bad path disables logging rather than taking the app down).
    """
    global ENABLED, HOT, _configured, _log_path
    if _configured:
        return _log_path
    _configured = True

    level = _env_level()
    if level is None:
        return None

    path = _resolve_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
    except OSError:
        return None
    handler.setFormatter(JsonLineFormatter())

    root = logging.getLogger("lazyagent")
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False

    ENABLED = True
    HOT = level <= logging.DEBUG
    _log_path = path

    event(
        log,
        "log.start",
        path=str(path),
        # not `level=` — that is this helper's own parameter
        log_level=logging.getLevelName(level),
        pid=os.getpid(),
        hot=HOT,
    )
    return path


def is_enabled() -> bool:
    return ENABLED


def log_path() -> Path | None:
    return _log_path


def next_id(prefix: str = "s") -> str:
    """A short correlation id, for stitching one multi-step path together."""
    global _seq
    with _seq_lock:
        _seq += 1
        return f"{prefix}{_seq}"


# ----------------------------------------------------------------------
# Emitting
# ----------------------------------------------------------------------


def event(
    logger: logging.Logger, name: str, /, level: int = logging.INFO, **fields
) -> None:
    """Emit one structured event. Returns immediately when logging is off."""
    if not ENABLED:
        return
    logger.log(level, name, extra={"lz_event": name, "lz_fields": fields})


def debug_event(logger: logging.Logger, name: str, /, **fields) -> None:
    """Emit one structured DEBUG event (hot paths)."""
    if not HOT:
        return
    logger.log(logging.DEBUG, name, extra={"lz_event": name, "lz_fields": fields})


@contextmanager
def timed(logger: logging.Logger, name: str, /, level: int = logging.INFO, **fields):
    """Time a block and emit ``<name>`` with ``duration_ms`` when it leaves.

    Yields a dict; anything put in it is merged into the emitted event, so a
    block can report what it actually found::

        with diag.timed(log, "worker.diff", worktree=path) as span:
            text = get_diff(path)
            span["bytes"] = len(text)
    """
    if not ENABLED:
        yield {}
        return
    extra: dict = {}
    start = time.perf_counter()
    failed = False
    try:
        yield extra
    except BaseException:
        failed = True
        raise
    finally:
        fields.update(extra)
        fields["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        if failed:
            fields["failed"] = True
        logger.log(level, name, extra={"lz_event": name, "lz_fields": fields})


class Gauge:
    """Counts how many callers are inside a block at once.

    For work that is *supposed* to be serialised, the occupancy is the finding:
    ``with GAUGE as n`` yields the count including this caller, and ``.peak``
    remembers the worst it ever got. No-ops when logging is off.
    """

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()
        self.peak = 0

    def __enter__(self) -> int:
        if not ENABLED:
            return 0
        with self._lock:
            self._n += 1
            if self._n > self.peak:
                self.peak = self._n
            return self._n

    def __exit__(self, *exc) -> None:
        if not ENABLED:
            return
        with self._lock:
            self._n -= 1


def wrap_timer(name: str, callback):
    """Wrap a ``set_interval``/``set_timer`` callback with entry/exit traces.

    Returns the callback unchanged when logging is off, so a disabled run has
    exactly the call it had before.
    """
    if not ENABLED:
        return callback

    def _wrapped():
        event(log, f"{name}.enter")
        start = time.perf_counter()
        try:
            return callback()
        finally:
            event(
                log,
                f"{name}.exit",
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )

    return _wrapped


# ----------------------------------------------------------------------
# Event-loop watchdog
# ----------------------------------------------------------------------


class _Watchdog:
    """Detects main-thread/event-loop stalls and snapshots the guilty stack.

    A coroutine on the loop stamps a heartbeat every ``poll_ms``. A daemon
    thread — which the stall cannot touch — watches that stamp go stale. When
    the gap crosses ``threshold_ms`` it grabs the loop thread's stack *while it
    is still blocked*, which is the whole point: that snapshot names whatever
    is hogging the pump.
    """

    def __init__(self, threshold_ms: float = 100.0, poll_ms: float = 25.0) -> None:
        self.threshold = threshold_ms / 1000.0
        self.poll = poll_ms / 1000.0
        self._last = time.perf_counter()
        self._loop_thread_id: int | None = None
        self._stop = threading.Event()
        self._task = None
        self._thread: threading.Thread | None = None
        self._blocked = False
        self._peak = 0.0

    def start(self) -> None:
        self._loop_thread_id = threading.get_ident()
        self._last = time.perf_counter()
        self._task = asyncio.create_task(self._beat())
        self._thread = threading.Thread(
            target=self._watch, name="lz-watchdog", daemon=True
        )
        self._thread.start()
        event(
            log,
            "watchdog.start",
            threshold_ms=round(self.threshold * 1000, 1),
            poll_ms=round(self.poll * 1000, 1),
        )

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        event(log, "watchdog.stop")

    async def _beat(self) -> None:
        try:
            while True:
                self._last = time.perf_counter()
                await asyncio.sleep(self.poll)
        except asyncio.CancelledError:
            pass

    def _watch(self) -> None:
        while not self._stop.wait(self.poll):
            gap = time.perf_counter() - self._last
            if gap > self.threshold:
                self._peak = max(self._peak, gap)
                if not self._blocked:
                    self._blocked = True
                    event(
                        log,
                        "loop.blocked",
                        level=logging.WARNING,
                        blocked_ms=round(gap * 1000, 1),
                        stack=self._loop_stack(),
                    )
            elif self._blocked:
                event(
                    log,
                    "loop.unblocked",
                    level=logging.WARNING,
                    blocked_ms=round(self._peak * 1000, 1),
                )
                self._blocked = False
                self._peak = 0.0

    def _loop_stack(self) -> list[str]:
        if self._loop_thread_id is None:
            return []
        try:
            frame = sys._current_frames().get(self._loop_thread_id)
            if frame is None:
                return []
            return [
                f"{fs.filename}:{fs.lineno} {fs.name}"
                for fs in traceback.extract_stack(frame)[-_STACK_DEPTH:]
            ]
        except Exception:
            return []


_watchdog: _Watchdog | None = None


def start_watchdog() -> None:
    """Start the event-loop watchdog. Must be called from the loop thread."""
    global _watchdog
    if not ENABLED or _watchdog is not None:
        return
    try:
        threshold = float(os.environ.get("LAZYAGENT_LOG_WATCHDOG_MS", "100"))
    except ValueError:
        threshold = 100.0
    _watchdog = _Watchdog(threshold_ms=threshold)
    _watchdog.start()


def stop_watchdog() -> None:
    global _watchdog
    if _watchdog is None:
        return
    _watchdog.stop()
    _watchdog = None
