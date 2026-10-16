"""PtyEmulator — inlined PTY emulator replacing textual-terminal.

Provides PTY forking and async I/O. Output is queued as raw bytes; decoding
is the consumer's job (see ``lazyagent.engine.session``).
Based on textual-terminal's TerminalEmulator with improvements:
- Robust stop() with process group kill and fd cleanup
- The child's exit code is kept (``returncode``)
- No ResourceWarning — fd is properly closed
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import shlex
import signal
import struct
import termios
import time
from pathlib import Path

from lazyagent import diagnostics as diag

log = logging.getLogger(__name__)

# Constants previously imported from textual-terminal
DECSET_PREFIX = "\x1b[?"
RE_ANSI_SEQUENCE = re.compile(r"(\x1b\[\??[\d;]*[a-zA-Z])")


class PtyEmulator:
    """Manages a PTY subprocess with async I/O queues."""

    def __init__(self, command: str) -> None:
        self._command = command
        self.ncol = 80
        self.nrow = 24
        self.run_task: asyncio.Task | None = None
        # Exit code once reaped: negative for a signal, as in subprocess.
        self.returncode: int | None = None

        self.fd = self._open_terminal(command)
        self.pid: int  # set by _open_terminal
        self.p_out = os.fdopen(self.fd, "w+b", 0)
        # Inbound commands (stdin bytes, resizes) and outbound
        # ``["stdout", bytes]`` / ``["disconnect", 1]`` messages.
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.send_queue: asyncio.Queue = asyncio.Queue()

    def start(self) -> None:
        """Create the async I/O task."""
        diag.event(log, "pty.start", pid=self.pid, command=self._command)
        self.run_task = asyncio.create_task(self._run())

    def stop(self) -> None:
        """Cancel tasks, kill the process group, close the fd."""
        # Timed because this runs on the message pump and blocks on waitpid().
        started = time.perf_counter()
        if self.run_task is not None:
            self.run_task.cancel()

        # Kill the entire process group (pty.fork children have pgid == pid)
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Reap the process
        if self.returncode is None:
            try:
                rpid, status = os.waitpid(self.pid, os.WNOHANG)
                if rpid == 0:
                    try:
                        os.killpg(self.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
                    _, status = os.waitpid(self.pid, 0)
                self.returncode = os.waitstatus_to_exitcode(status)
            except ChildProcessError:
                pass

        # Remove event-loop reader and close fd
        try:
            asyncio.get_event_loop().remove_reader(self.p_out)
        except Exception:
            pass
        try:
            self.p_out.close()
        except OSError:
            pass

        diag.event(
            log,
            "pty.stop",
            pid=self.pid,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    def poll(self) -> int | None:
        """Reap the child if it has exited; return its exit code or None."""
        if self.returncode is None:
            try:
                rpid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if rpid != 0:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    async def wait_exit(self, timeout: float) -> int | None:
        """Poll for the child's exit for up to ``timeout`` seconds.

        The PTY reports EOF as soon as the child's side of it closes, which
        can be a moment before the child is reapable.
        """
        deadline = time.monotonic() + timeout
        while self.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        return self.returncode

    def _open_terminal(self, command: str) -> int:
        """Fork a PTY and exec the command in the child."""
        forked = time.perf_counter()
        self.pid, fd = pty.fork()
        if self.pid == 0:
            argv = shlex.split(command)
            env = dict(TERM="xterm", LC_ALL="en_US.UTF-8", HOME=str(Path.home()))
            os.execvpe(argv[0], argv, env)
        diag.event(
            log,
            "pty.fork",
            pid=self.pid,
            command=command,
            duration_ms=round((time.perf_counter() - forked) * 1000, 2),
        )
        return fd

    async def _write_all(self, data: bytes) -> None:
        """Write every byte of ``data`` to the PTY master.

        ``p_out`` is opened with ``buffering=0``, so ``write`` is a single
        ``write(2)`` and is allowed to report a short count. Taking that
        count for granted silently truncates the payload — for stdin that
        means half an instruction, or a dropped submit key.
        """
        view = memoryview(data)
        while view:
            try:
                written = self.p_out.write(view)
            except BlockingIOError:
                written = None
            if written is None:
                # Would block: yield and retry rather than drop the tail.
                await asyncio.sleep(0.005)
                continue
            if written <= 0:
                log.warning("PTY write made no progress; %d bytes dropped", len(view))
                return
            view = view[written:]

    async def _run(self) -> None:
        """Main I/O loop: queue output as raw bytes, apply inbound commands."""
        loop = asyncio.get_running_loop()

        # Optional raw-stream capture for debugging terminal-rendering issues.
        # Set LAZYAGENT_PTY_CAPTURE=/path/to/file to log every byte the PTY
        # emits to that file. Append mode; safe to leave on across runs.
        # A bad path must not take down the agent: log and continue without
        # capture rather than letting OSError propagate out of _run.
        capture_path = os.environ.get("LAZYAGENT_PTY_CAPTURE")
        capture_file = None
        if capture_path:
            try:
                capture_file = open(capture_path, "ab", buffering=0)
            except OSError as e:
                log.warning(
                    "LAZYAGENT_PTY_CAPTURE: failed to open %s: %s — disabling capture",
                    capture_path,
                    e,
                )

        def on_output():
            try:
                raw = self.p_out.read(65536)
                if capture_file is not None:
                    # Capture is best-effort debug output; don't let it
                    # break the read loop. A close/cancel race can leave
                    # the reader callback firing after the file is closed,
                    # which raises ValueError, not OSError.
                    try:
                        capture_file.write(raw)
                    except (OSError, ValueError):
                        pass
            except Exception:
                raw = b""
            if raw:
                self.send_queue.put_nowait(["stdout", raw])
            else:
                # EIO (Linux) or EOF: the child's side of the PTY is gone.
                loop.remove_reader(self.p_out)
                self.send_queue.put_nowait(["disconnect", 1])

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        try:
            while True:
                msg = await self.recv_queue.get()
                if msg[0] == "stdin":
                    data = msg[1]
                    if isinstance(data, str):
                        data = data.encode()
                    await self._write_all(data)
                elif msg[0] == "set_size":
                    winsize = struct.pack("HH", msg[1], msg[2])
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except asyncio.CancelledError:
            pass
        finally:
            if capture_file is not None:
                try:
                    capture_file.close()
                except OSError:
                    pass
