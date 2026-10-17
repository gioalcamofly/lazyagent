"""Start a repo's engine detached from the terminal, and find it again.

The TUI (auto-start, D4) and ``lazyagent engine start`` both come here. The
engine runs as ``python -m lazyagent.engine --detached --ready-fd N repo`` in
a session of its own (``setsid``: no controlling terminal, so closing the
terminal or tmux pane that started it does not reach it), stdin from
``/dev/null`` and stdout/stderr appended to its log in the state dir
(:func:`~lazyagent.engine.runtime.log_path`). It writes ``ready`` to the
pipe once it serves its v1 socket; an engine that exits first (it lost the
repo lock to another one, or failed) closes the pipe without it, and the
caller then waits for whichever engine does serve the repo.

The log is capped: the engine rotates it at :data:`LOG_MAX_BYTES` and keeps
one old file, so a detached engine never fills the disk; the launcher
rotates a full log before a start, too.

Standard library and :mod:`lazyagent.engine.runtime` only, so a TUI can
start an engine without importing one.
"""

from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from lazyagent.engine.runtime import (
    INFO_NAME,
    LOCK_NAME,
    lock_holder,
    log_path,
    read_info,
    runtime_dir,
)

START_TIMEOUT = 10.0
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 1
READY = b"ready"
# Variables of the starting client that mean nothing to an engine, or would
# make it think it is something it is not.
_CLIENT_ONLY_ENV = ("LAZYAGENT_ENGINE", "LAZYAGENT_ENGINE_SOCKET", "LAZYAGENT_HOST")
_POLL = 0.01
_PROBE_TIMEOUT = 0.5


class EngineStartError(RuntimeError):
    """The engine could not be started; the message says why and where its
    log is."""


def resolve_repo_root(repo_path: str | None) -> str:
    """The repo root an engine for ``repo_path`` is named after, as the
    engine computes it: the path itself, resolved, else the top level of
    the git repo around the cwd. Raises ``WorktreeManagerError``."""
    if repo_path:
        return str(Path(repo_path).resolve())
    from lazyagent.worktree_manager import find_repo_root

    return str(find_repo_root())


def find(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path | None:
    """The socket of a live engine serving ``repo_root``, or None.

    Like :func:`~lazyagent.engine.client.discover`, but a socket nobody
    listens on (a killed engine's) counts as none.
    """
    info = read_info(runtime_dir(repo_root, environ) / INFO_NAME)
    if info is None or info.get("repo_root") != repo_root:
        return None
    path = info.get("socket")
    if not isinstance(path, str) or not alive(path):
        return None
    return Path(path)


def alive(path: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Whether a process accepts connections on the Unix socket ``path``."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(path)
        except socket.timeout:
            return True  # too busy to accept in time, but there
        except OSError:
            return False
    return True


def info(repo_root: str, environ: Mapping[str, str] = os.environ) -> dict | None:
    """``engine.json`` of the engine serving ``repo_root``, when it lives."""
    if find(repo_root, environ) is None:
        return None
    return read_info(runtime_dir(repo_root, environ) / INFO_NAME)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Move a log over ``max_bytes`` to ``<path>.1`` (replacing it)."""
    try:
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def tail(path: Path, lines: int = 20) -> str:
    """The last ``lines`` lines of a text file ("" when unreadable)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            data = f.read()
    except OSError:
        return ""
    return "\n".join(data.decode(errors="replace").splitlines()[-lines:])


@dataclass
class Launch:
    """An engine process being started; :meth:`wait` for it to serve."""

    repo_root: str
    proc: subprocess.Popen
    log: Path
    environ: Mapping[str, str]
    _ready_fd: int | None
    started_at: float

    @property
    def pid(self) -> int:
        return self.proc.pid

    def wait(self, timeout: float = START_TIMEOUT) -> Path:
        """Block until the repo is served (by this engine, or by another
        that won the lock); its socket. Raises :class:`EngineStartError`."""
        deadline = self.started_at + timeout
        line = self._read_ready(deadline)
        if line.startswith(READY):
            path = find(self.repo_root, self.environ)
            if path is not None:
                return path
        # It exited without serving: lost the lock, or failed.
        path = self._wait_for_other(deadline)
        if path is not None:
            return path
        try:
            code = self.proc.wait(max(0.0, min(deadline - time.monotonic(), 2.0)))
        except subprocess.TimeoutExpired:
            code = None
        reason = (
            f"it exited with code {code}"
            if code is not None
            else f"it did not get ready within {timeout:g}s"
        )
        last = tail(self.log, 8)
        raise EngineStartError(
            f"could not start an engine for {self.repo_root}: {reason}"
            + (f"\n{last}" if last else "")
            + f"\n(log: {self.log})"
        )

    def close(self) -> None:
        """Stop waiting (the engine itself keeps going)."""
        fd, self._ready_fd = self._ready_fd, None
        if fd is not None:
            os.close(fd)

    def _read_ready(self, deadline: float) -> bytes:
        fd, self._ready_fd = self._ready_fd, None
        if fd is None:
            return b""
        buf = b""
        try:
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([fd], [], [], remaining)
                if not readable:
                    break
                chunk = os.read(fd, 256)
                if not chunk:
                    break
                buf += chunk
        finally:
            os.close(fd)
        return buf

    def _wait_for_other(self, deadline: float) -> Path | None:
        """The socket of the engine that holds the repo lock, once it
        serves; None when nobody holds it (or past ``deadline``)."""
        lock = runtime_dir(self.repo_root, self.environ) / LOCK_NAME
        while True:
            path = find(self.repo_root, self.environ)
            if path is not None:
                return path
            holder = lock_holder(lock)
            if holder is None or holder == self.pid or not _pid_alive(holder):
                return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL)


def _reap(proc: subprocess.Popen) -> None:
    # The engine outlives the caller as a rule; while the caller lives, its
    # exit must not leave a zombie behind.
    threading.Thread(
        target=proc.wait, name="lazyagent-engine-reaper", daemon=True
    ).start()


def spawn(
    repo_root: str,
    environ: Mapping[str, str] = os.environ,
    *,
    python: str = sys.executable,
) -> Launch:
    """Start a detached engine for ``repo_root`` (without waiting)."""
    log = log_path(repo_root, environ)
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rotate_log(log)
    env = {k: v for k, v in environ.items() if k not in _CLIENT_ONLY_ENV}
    read_fd, write_fd = os.pipe()
    try:
        with open(log, "ab", buffering=0) as out:
            os.chmod(log, 0o600)
            proc = subprocess.Popen(
                [
                    python,
                    "-m",
                    "lazyagent.engine",
                    "--detached",
                    "--ready-fd",
                    str(write_fd),
                    repo_root,
                ],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                pass_fds=(write_fd,),
                start_new_session=True,
                cwd="/",
                env=env,
            )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    _reap(proc)
    return Launch(repo_root, proc, log, environ, read_fd, time.monotonic())


def ensure(
    repo_root: str,
    environ: Mapping[str, str] = os.environ,
    *,
    timeout: float = START_TIMEOUT,
) -> tuple[Path, Launch | None]:
    """The socket of the engine serving ``repo_root``, starting one when
    none does; the :class:`Launch` when this call started it."""
    path = find(repo_root, environ)
    if path is not None:
        return path, None
    launch = spawn(repo_root, environ)
    return launch.wait(timeout), launch


def prestart(
    repo_root: str, environ: Mapping[str, str] = os.environ
) -> Launch | None:
    """Start an engine for ``repo_root`` when none serves it, without
    waiting: a TUI calls this first thing, so the engine boots while the TUI
    imports and lays out its screen."""
    if find(repo_root, environ) is not None:
        return None
    return spawn(repo_root, environ)
