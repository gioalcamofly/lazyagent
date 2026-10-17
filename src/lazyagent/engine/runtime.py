"""Where an engine's v1 socket lives, computable from the repo root alone.

Standard library only, so the reference client can find an engine without
importing the server. ``docs/design/engine-split.md`` §4f is the spec.

The same dir holds ``engine.lock``: the engine that serves a repo holds an
``flock`` on it for as long as it runs (:class:`EngineLock`), so there is
one engine per repo. A detached engine's output goes to a log in the state
dir (:func:`log_path`), which outlives logouts, unlike the runtime dir.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SOCKET_NAME = "engine.sock"
INFO_NAME = "engine.json"
LOCK_NAME = "engine.lock"


def repo_digest(repo_root: str) -> str:
    return hashlib.sha1(repo_root.encode()).hexdigest()[:12]


def runtime_dir(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    """The per-repo runtime dir (§4f)."""
    digest = repo_digest(repo_root)
    base = environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "lazyagent" / digest
    return Path(tempfile.gettempdir()) / f"lazyagent-{os.getuid()}" / digest


def state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """``$XDG_STATE_HOME/lazyagent`` (``~/.local/state/lazyagent``)."""
    base = environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "lazyagent"


def log_path(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    """Where a detached engine for ``repo_root`` writes its output."""
    return state_dir(environ) / "engines" / f"{repo_digest(repo_root)}.log"


def socket_path(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    return runtime_dir(repo_root, environ) / SOCKET_NAME


def read_info(path: Path) -> dict[str, Any] | None:
    """``engine.json``, or None when missing or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_empty_dirs(directory: Path) -> None:
    """Remove the repo's runtime dir, then the per-user one, if empty."""
    for path in (directory, directory.parent):
        try:
            os.rmdir(path)
        except OSError:
            break


class EngineLock:
    """The single-instance lock of a repo's engine: an ``flock`` on
    ``engine.lock`` in its runtime dir, held while the engine runs.

    The file holds the holder's pid, for messages. The descriptor is not
    inherited by children (agents, shells), so the lock dies with the
    engine process even when its agents outlive it for a moment.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock without waiting; False when another process has
        it. The runtime dir must exist."""
        if self._fd is not None:
            return True
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                os.close(fd)
                if e.errno in (errno.EWOULDBLOCK, errno.EACCES):
                    return False
                raise
            # The holder unlinks the file on release: a lock taken on a file
            # that is no longer at the path guards nothing, so try again.
            try:
                same = os.stat(self.path).st_ino == os.fstat(fd).st_ino
            except FileNotFoundError:
                same = False
            if same:
                break
            os.close(fd)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        """Unlink the file, then drop the lock (in that order, so a waiter
        never locks a file that is about to vanish unnoticed)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            self.path.unlink(missing_ok=True)
        finally:
            os.close(fd)


def lock_holder(path: str | os.PathLike[str]) -> int | None:
    """The pid written in a lock file, or None."""
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None
