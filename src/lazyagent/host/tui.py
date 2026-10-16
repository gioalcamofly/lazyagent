"""The hosted TUI: ``lazyagent`` running on a PTY the host owns.

The child gets its own session with the PTY as controlling terminal
(util-linux ``setsid --ctty``, as the engine's sessions do), so a resize of
the PTY reaches it as SIGWINCH and Ctrl+C in the host's terminal does not.
Its output goes to ``on_output`` unchanged; on the way the host picks up the
OSC 7717 announcement (:mod:`lazyagent.protocol.hosted`) and the private
modes the TUI turned on, which a page that connects late needs replayed.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import signal
import struct
import termios
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from lazyagent.protocol.hosted import OSC_NUMBER, OSC_TAG, parse_osc

_READ_SIZE = 64 * 1024
_DECSET_RE = re.compile(rb"\x1b\[\?([\d;]+)([hl])")
_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC_PREFIX = f"\x1b]{OSC_NUMBER};{OSC_TAG};".encode()
# Longest escape sequence kept back to wait for the rest of it.
_MAX_CARRY = 4096
# Synchronized output: replaying it would freeze a terminal until reset.
_NOT_REPLAYED = {2026}


def _incomplete_tail(data: bytes) -> int:
    """Length of the escape sequence ``data`` ends in the middle of."""
    start = data.rfind(b"\x1b")
    if start < 0 or len(data) - start > _MAX_CARRY:
        return 0
    tail = data[start:]
    if len(tail) == 1:
        return 1
    if tail[1:2] == b"[":
        return 0 if _CSI_RE.match(tail) else len(tail)
    if tail[1:2] == b"]":
        return 0 if b"\x07" in tail else len(tail)
    return 0


class HostedTui:
    """``argv`` on a new PTY of ``rows`` × ``cols``."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        on_output: Callable[[bytes], None],
        on_announce: Callable[[dict[str, Any]], None] = lambda payload: None,
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env)
        self.on_output = on_output
        self.on_announce = on_announce
        self.rows = rows
        self.cols = cols
        self.process: asyncio.subprocess.Process | None = None
        self.returncode: int | None = None
        # DECSET private modes as last set (True) or reset (False).
        self.modes: dict[int, bool] = {}
        self._fd: int | None = None
        self._carry = b""
        self._eof = asyncio.Event()
        self._exited = asyncio.Event()
        self._redraw: asyncio.Task | None = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    @property
    def running(self) -> bool:
        return self.process is not None and self.returncode is None

    async def start(self) -> None:
        master, slave = os.openpty()
        try:
            self._set_size(master, self.rows, self.cols)
            self.process = await asyncio.create_subprocess_exec(
                "setsid",
                "--ctty",
                *self.argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self.cwd,
                env=self.env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            # Only the child holds it now, so its exit is the PTY's EOF.
            os.close(slave)
        os.set_blocking(master, False)
        self._fd = master
        asyncio.get_running_loop().add_reader(master, self._on_readable)
        asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        # Take what is still in the PTY before calling it gone.
        try:
            await asyncio.wait_for(self._eof.wait(), 1.0)
        except asyncio.TimeoutError:
            pass
        self.returncode = code
        self._close_fd()
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    # -- output ------------------------------------------------------------

    def _on_readable(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, _READ_SIZE)
        except BlockingIOError:
            return
        except OSError:  # EIO once the child side is closed
            data = b""
        if not data:
            asyncio.get_running_loop().remove_reader(self._fd)
            self._eof.set()
            return
        self._scan(data)
        self.on_output(data)

    def _scan(self, data: bytes) -> None:
        buf = self._carry + data
        keep = _incomplete_tail(buf)
        if not keep:
            # A chunk may also end inside the OSC prefix before its ESC ].
            for n in range(len(_OSC_PREFIX) - 1, 0, -1):
                if buf.endswith(_OSC_PREFIX[:n]):
                    keep = n
                    break
        complete, self._carry = buf[: len(buf) - keep], buf[len(buf) - keep :]
        for match in _DECSET_RE.finditer(complete):
            on = match.group(2) == b"h"
            for mode in match.group(1).split(b";"):
                if mode:
                    self.modes[int(mode)] = on
        if _OSC_PREFIX in complete:
            for payload in parse_osc(complete):
                self.on_announce(payload)

    def mode_sequence(self) -> bytes:
        """The DECSET/DECRST sequences that put a fresh terminal in the
        modes the TUI set (alternate screen first, so the rest apply to it)."""
        modes = sorted(
            (m for m in self.modes if m not in _NOT_REPLAYED),
            key=lambda m: m not in (47, 1047, 1049),
        )
        return b"".join(
            b"\x1b[?%d%s" % (m, b"h" if self.modes[m] else b"l") for m in modes
        )

    # -- input and size ----------------------------------------------------

    def write(self, data: bytes) -> None:
        """Keystrokes for the TUI. Short writes are retried; a full PTY
        drops the rest rather than block the host."""
        if self._fd is None:
            return
        view = memoryview(data)
        for _ in range(100):
            if not view:
                return
            try:
                view = view[os.write(self._fd, view) :]
            except BlockingIOError:
                continue
            except OSError:
                return

    @staticmethod
    def _set_size(fd: int, rows: int, cols: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY; the kernel tells the TUI (SIGWINCH) if it changed."""
        self.rows, self.cols = rows, cols
        if self._fd is not None:
            self._set_size(self._fd, rows, cols)

    def redraw(self) -> None:
        """Make the TUI repaint the whole screen: a resize one column
        narrower and back, which it answers with a full refresh."""
        if self._fd is None or (self._redraw and not self._redraw.done()):
            return

        async def nudge() -> None:
            rows, cols = self.rows, self.cols
            if self._fd is None:
                return
            self._set_size(self._fd, rows, max(cols - 1, 1))
            await asyncio.sleep(0.1)
            if self._fd is not None:
                self._set_size(self._fd, self.rows, self.cols)

        self._redraw = asyncio.get_running_loop().create_task(nudge())

    # -- stopping ----------------------------------------------------------

    def _signal(self, signum: int) -> None:
        if self.process is None or self.returncode is not None:
            return
        # Its own session, so its own process group.
        for send in (os.killpg, os.kill):
            try:
                send(self.process.pid, signum)
            except (ProcessLookupError, PermissionError):
                pass

    async def stop(self, *, grace: float = 5.0, term_grace: float = 3.0) -> int | None:
        """Quit the TUI: Ctrl+Q (its own clean quit, which stops its
        agents), then SIGTERM, then SIGKILL."""
        if self.process is None:
            return None
        steps = [
            (lambda: self.write(b"\x11"), grace),
            (lambda: self._signal(signal.SIGTERM), term_grace),
            (lambda: self._signal(signal.SIGKILL), 3.0),
        ]
        for act, timeout in steps:
            if self._exited.is_set():
                break
            act()
            try:
                await asyncio.wait_for(self._exited.wait(), timeout)
            except asyncio.TimeoutError:
                continue
        return self.returncode

    def _close_fd(self) -> None:
        if self._fd is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(self._fd)
        except (RuntimeError, ValueError):
            pass
        os.close(self._fd)
        self._fd = None
