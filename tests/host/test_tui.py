"""HostedTui: a program on the host's PTY, with a scripted child instead of
the real TUI."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import textwrap

import pytest

from lazyagent.host.tui import HostedTui, _incomplete_tail

pytestmark = pytest.mark.asyncio

CHILD = textwrap.dedent(
    r"""
    import fcntl, os, signal, struct, sys, termios, time

    def size():
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        return rows, cols

    signal.signal(signal.SIGWINCH, lambda *_: os.write(1, b"SIZE %d %d\n" % size()))
    if "ignore-term" in sys.argv:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # Modes, one CSI and the OSC announcement split across writes.
    os.write(1, b"\x1b[?1049h\x1b[?1000;1006h\x1b[?25l\x1b[?20")
    time.sleep(0.05)
    os.write(1, b"26h\x1b[?1000l\x1b]7717;lazy")
    time.sleep(0.05)
    os.write(1, b'agent;{"v":1,"socket":"/x.sock"}\x07')
    os.write(1, b"READY %d %d sid=%d\n" % (*size(), os.getsid(0)))
    line = sys.stdin.readline()
    os.write(1, b"GOT " + line.encode())
    while True:
        time.sleep(1)
    """
)


class Run:
    def __init__(self, *args: str, rows: int = 30, cols: int = 100) -> None:
        self.output = b""
        self.announced: list[dict] = []
        self.changed = asyncio.Event()
        self.tui = HostedTui(
            [sys.executable, "-c", CHILD, *args],
            cwd=os.getcwd(),
            env=dict(os.environ),
            on_output=self._on_output,
            on_announce=self.announced.append,
            rows=rows,
            cols=cols,
        )

    def _on_output(self, data: bytes) -> None:
        self.output += data
        self.changed.set()

    async def wait_for(self, needle: bytes, timeout: float = 10) -> None:
        async def find() -> None:
            while needle not in self.output:
                self.changed.clear()
                await self.changed.wait()

        try:
            await asyncio.wait_for(find(), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"{needle!r} not in {self.output!r}") from None


async def test_runs_on_its_own_pty_session_and_scans_its_output():
    run = Run()
    await run.tui.start()
    try:
        await run.wait_for(b"READY")
        pid = run.tui.pid
        assert b"READY 30 100 sid=%d" % pid in run.output
        assert run.announced == [{"v": 1, "socket": "/x.sock"}]
        assert run.tui.modes == {
            1049: True,
            1000: False,
            1006: True,
            25: False,
            2026: True,
        }
        # Alternate screen first; synchronized output is never replayed.
        assert run.tui.mode_sequence() == b"\x1b[?1049h\x1b[?1000l\x1b[?1006h\x1b[?25l"

        run.tui.write(b"hello\r")
        await run.wait_for(b"GOT hello")

        run.tui.resize(40, 120)
        await run.wait_for(b"SIZE 40 120")
        run.tui.redraw()
        await run.wait_for(b"SIZE 40 119")
        await asyncio.sleep(0.3)
        assert run.output.endswith(b"SIZE 40 120\r\n")
    finally:
        code = await run.tui.stop(grace=0.2)
    assert code == -signal.SIGTERM
    assert not run.tui.running
    assert await run.tui.wait() == -signal.SIGTERM


async def test_stop_kills_a_child_that_ignores_sigterm():
    run = Run("ignore-term")
    await run.tui.start()
    await run.wait_for(b"READY")
    pid = run.tui.pid
    assert await run.tui.stop(grace=0.1, term_grace=0.3) == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_child_exit_is_seen_after_its_output():
    seen = []
    tui = HostedTui(
        [sys.executable, "-c", "print('bye'); raise SystemExit(3)"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        on_output=seen.append,
    )
    await tui.start()
    assert await asyncio.wait_for(tui.wait(), 10) == 3
    assert b"bye" in b"".join(seen)
    # Nothing left to do, and writes to a closed PTY are dropped.
    tui.write(b"ignored")
    assert await tui.stop() == 3


@pytest.mark.parametrize(
    ("data", "kept"),
    [
        (b"plain", 0),
        (b"x\x1b", 1),
        (b"x\x1b[?10", 5),
        (b"x\x1b[?1000h", 0),
        (b"x\x1b]7717;lazyagent;{", 18),
        (b"x\x1b]0;title\x07", 0),
        (b"x\x1b(B", 0),
    ],
)
async def test_incomplete_tail(data, kept):
    assert _incomplete_tail(data) == kept
