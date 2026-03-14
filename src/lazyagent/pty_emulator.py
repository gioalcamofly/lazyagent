"""PtyEmulator — inlined PTY emulator replacing textual-terminal.

Provides PTY forking, async I/O, and incremental UTF-8 decoding.
Based on textual-terminal's TerminalEmulator with improvements:
- Incremental UTF-8 decoding (no data loss on multi-byte splits)
- Robust stop() with process group kill and fd cleanup
- No ResourceWarning — fd is properly closed
"""

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import re
import shlex
import signal
import struct
import termios
from pathlib import Path

# Constants previously imported from textual-terminal
DECSET_PREFIX = "\x1b[?"
RE_ANSI_SEQUENCE = re.compile(r"(\x1b\[\??[\d;]*[a-zA-Z])")


class PtyEmulator:
    """Manages a PTY subprocess with async I/O queues."""

    def __init__(self, command: str) -> None:
        self.ncol = 80
        self.nrow = 24
        self.data_or_disconnect: str | None = None
        self.run_task: asyncio.Task | None = None
        self.send_task: asyncio.Task | None = None

        self.fd = self._open_terminal(command)
        self.pid: int  # set by _open_terminal
        self.p_out = os.fdopen(self.fd, "w+b", 0)
        self.recv_queue: asyncio.Queue = asyncio.Queue()
        self.send_queue: asyncio.Queue = asyncio.Queue()
        self.event = asyncio.Event()

    def start(self) -> None:
        """Create the async I/O tasks."""
        self.run_task = asyncio.create_task(self._run())
        self.send_task = asyncio.create_task(self._send_data())

    def stop(self) -> None:
        """Cancel tasks, kill the process group, close the fd."""
        # Cancel async tasks
        if self.run_task is not None:
            self.run_task.cancel()
        if self.send_task is not None:
            self.send_task.cancel()

        # Kill the entire process group (pty.fork children have pgid == pid)
        try:
            os.killpg(self.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        # Reap the process
        try:
            rpid, _ = os.waitpid(self.pid, os.WNOHANG)
            if rpid == 0:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                os.waitpid(self.pid, 0)
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

    def _open_terminal(self, command: str) -> int:
        """Fork a PTY and exec the command in the child."""
        self.pid, fd = pty.fork()
        if self.pid == 0:
            argv = shlex.split(command)
            env = dict(TERM="xterm", LC_ALL="en_US.UTF-8", HOME=str(Path.home()))
            os.execvpe(argv[0], argv, env)
        return fd

    async def _run(self) -> None:
        """Main I/O loop with incremental UTF-8 decoding."""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        loop = asyncio.get_running_loop()

        def on_output():
            try:
                raw = self.p_out.read(65536)
                self.data_or_disconnect = decoder.decode(raw)
                self.event.set()
            except Exception:
                loop.remove_reader(self.p_out)
                self.data_or_disconnect = None
                self.event.set()

        loop.add_reader(self.p_out, on_output)
        await self.send_queue.put(["setup", {}])
        try:
            while True:
                msg = await self.recv_queue.get()
                if msg[0] == "stdin":
                    self.p_out.write(msg[1].encode())
                elif msg[0] == "set_size":
                    winsize = struct.pack("HH", msg[1], msg[2])
                    fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
                elif msg[0] == "click":
                    x = msg[1] + 1
                    y = msg[2] + 1
                    button = msg[3]
                    if button == 1:
                        self.p_out.write(f"\x1b[<0;{x};{y}M".encode())
                        self.p_out.write(f"\x1b[<0;{x};{y}m".encode())
                elif msg[0] == "scroll":
                    x = msg[2] + 1
                    y = msg[3] + 1
                    if msg[1] == "up":
                        self.p_out.write(f"\x1b[<64;{x};{y}M".encode())
                    if msg[1] == "down":
                        self.p_out.write(f"\x1b[<65;{x};{y}M".encode())
        except asyncio.CancelledError:
            pass

    async def _send_data(self) -> None:
        """Forward decoded output or disconnect signal to the send queue."""
        try:
            while True:
                await self.event.wait()
                self.event.clear()
                if self.data_or_disconnect is not None:
                    await self.send_queue.put(["stdout", self.data_or_disconnect])
                else:
                    await self.send_queue.put(["disconnect", 1])
        except asyncio.CancelledError:
            pass
