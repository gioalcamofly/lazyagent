"""Terminal sessions: a PTY process and everything known about its output.

A :class:`TerminalSession` owns one PTY child for its whole life, whether or
not any client shows it:

- a ring buffer of the raw output bytes, addressed by offset (the position
  of a byte in everything the child has written since the session started);
- a headless :class:`ScrollbackScreen`, fed from every chunk. While a viewer
  is watching, each chunk is fed as it arrives; otherwise chunks are batched
  for :data:`UNWATCHED_FEED_INTERVAL` seconds and then fed by the loop's
  :class:`_Feeder` in slices, a few milliseconds of parsing at a time, so a
  burst of hidden output never holds the event loop (keys, frames and
  requests go between the slices). A session the feeder falls more than
  :data:`PENDING_FEED_LIMIT` bytes behind on keeps only the newest
  :data:`PENDING_FEED_KEEP` (see :meth:`TerminalSession.bound_pending`).
  The backlog is kept as raw bytes and :attr:`~TerminalSession.parsed_offset`
  says exactly how far the screen has got, so a snapshot can be taken without
  parsing the backlog first (:meth:`~TerminalSession.snapshot` with
  ``flush=False``): the snapshot is exact at that offset and the rest is in
  the ring;
- the terminal modes the child has set (mouse tracking, bracketed paste),
  which programmatic input needs to frame text correctly;
- :meth:`~TerminalSession.send_input`, which frames text as a paste and
  presses Enter only once the child has read it.

:class:`AgentSession` adds status detection: a provider observer fed with the
output, a debounced scan of the headless screen, hang detection and the
agent's ``status`` / ``confidence`` / ``detail``.

Status changes and the exit are delivered as :class:`StatusChanged` and
:class:`SessionExited` to plain callables (:meth:`TerminalSession.add_listener`).
Viewers get every output chunk with its offset (:meth:`TerminalSession.attach`).
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import pyte

from lazyagent import diagnostics as diag
from lazyagent.agent_observers import AgentLifecycleEvent, AgentObserver
from lazyagent.engine.screen import (
    SNAPSHOT_SCROLLBACK_LINES,
    ScrollbackScreen,
    ansi_snapshot,
)
from lazyagent.models import AgentStatus, LifecycleConfidence
from lazyagent.pty_emulator import DECSET_PREFIX, RE_ANSI_SEQUENCE, PtyEmulator

log = logging.getLogger(__name__)

DEFAULT_RING_CAPACITY = 4 * 1024 * 1024

# How often an unwatched session's screen catches up with its output. Nobody
# sees that screen; only the status scan and read_agent_output read it, and
# both tolerate a fraction of a second of staleness. Parsing every chunk of
# every hidden agent was the dominant idle cost.
UNWATCHED_FEED_INTERVAL = 0.25

# Parsing done in one go before the feeder yields to the loop, the size of
# each pyte.feed call within it, and the rest the feeder takes after a full
# slice: hidden output gets at most about half of the loop, whatever its
# rate, and keys and frames never wait more than a slice. Short turns: a
# key's echo crosses the loop several times and may wait behind a turn at
# each (1 ms turns halved the echo's p95 next to five chatty hidden agents,
# at the same parsing throughput as 4 ms ones).
FEED_BUDGET = 0.001
FEED_SLICE = 512
FEED_REST = 0.001

# A hidden session whose unparsed output passes the limit (in bytes) keeps
# only the newest part: its screen is only read by the status scan (which wants the
# latest screen) and read_agent_output (the last lines). Nobody waits on the
# rest, and parsing it would cost far more than the output it explains.
PENDING_FEED_LIMIT = 1024 * 1024
PENDING_FEED_KEEP = 256 * 1024

# How long the session waits for an exited child to become reapable after the
# PTY reports EOF, before it falls back to killing it.
_EXIT_REAP_TIMEOUT = 1.0

HANG_SECONDS = 600  # 10 minutes
_SCAN_DEBOUNCE_SECONDS = 0.15
# Lower bound between two consecutive screen scans. The debounce alone lets
# scans fire every ~150 ms when output is bursty; this caps the dispatch rate
# regardless of stream cadence so a long stream of short bursts doesn't keep
# scanning at full speed.
_SCAN_MIN_INTERVAL = 0.5

# ---------------------------------------------------------------------------
# Programmatic input framing
# ---------------------------------------------------------------------------
#
# Bracketed paste (DECSET 2004). A real terminal wraps pasted text in these
# markers so the app can tell "content the user pasted" apart from "keys the
# user pressed". That distinction is exactly what programmatic input needs:
# without it, agent CLIs built on Ink (Claude Code, Codex, Gemini) run their
# stdin chunk through a tokeniser that only splits control characters out as
# their own key when the whole chunk is short (< 64 chars). A longer chunk
# ending in CR is absorbed into one text run, so the text lands in the prompt
# and Enter never fires — the user has to press it by hand. Framing the text
# as a paste makes the trailing CR a token in its own right no matter how the
# kernel batches our writes.
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"

# Submit key. Enter is CR; LF is Shift+Enter (newline inside the prompt) for
# Claude Code and most line-editing TUIs.
SUBMIT_KEY = "\r"


def _env_seconds(name: str, default: float) -> float:
    """Read a non-negative float from the environment, else ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


# How long ``send_input`` waits for the child to show any sign of having read
# what we just wrote before it writes the submit key. Any PTY output is the
# acknowledgement: it means the child's input loop has run since our write, so
# the submit key will arrive in a later read() instead of being appended to the
# same chunk. Bounded, because a busy or silent agent must not stall the submit.
SUBMIT_ACK_TIMEOUT = _env_seconds("LAZYAGENT_SUBMIT_ACK_TIMEOUT", 2.0)

# Small floor applied after the acknowledgement. The child redraws as soon as
# it has *parsed* the paste, which can be a beat before its input state has
# committed; a submit key racing into the same dispatch batch would submit the
# prompt as it was before the paste. There is no observable signal for that
# commit, so this one is a delay by necessity — kept tiny and overridable.
SUBMIT_SETTLE_DELAY = _env_seconds("LAZYAGENT_SUBMIT_SETTLE_DELAY", 0.05)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusChanged:
    """An agent's status or detail changed."""

    worktree_path: str
    agent_id: str
    status: AgentStatus
    confidence: LifecycleConfidence
    detail: str
    last_output_time: float | None


@dataclass(frozen=True)
class SessionExited:
    """The child went away on its own (not through :meth:`stop`)."""

    worktree_path: str
    agent_id: str
    exit_code: int | None


SessionEvent = StatusChanged | SessionExited
SessionListener = Callable[[SessionEvent], None]
OutputListener = Callable[[int, bytes], None]
# (rows, cols, offset): the new size and the output offset it applies from.
SizeListener = Callable[[int, int, int], None]


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


class RingOffsetError(LookupError):
    """The requested offset is no longer (or not yet) in the ring."""


class ByteRing:
    """The last ``capacity`` bytes of a stream, addressed by stream offset.

    Storage grows with the data up to ``capacity`` and is reused circularly
    from then on, so a quiet session does not pay for the full ring.
    """

    def __init__(self, capacity: int = DEFAULT_RING_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._buf = bytearray()
        self._end = 0

    @property
    def start_offset(self) -> int:
        """Offset of the oldest byte still held."""
        return max(0, self._end - self.capacity)

    @property
    def end_offset(self) -> int:
        """Offset the next appended byte will get (= bytes written so far)."""
        return self._end

    def append(self, data: bytes) -> int:
        """Store ``data``; return the offset of its first byte."""
        offset = self._end
        size = len(data)
        if not size:
            return offset
        cap = self.capacity
        view = memoryview(data)
        if size > cap:
            view = view[size - cap :]
        self._end += size
        start = (self._end - len(view)) % cap
        if len(self._buf) < cap:
            if start == len(self._buf) and start + len(view) <= cap:
                self._buf += view
                return offset
            self._buf.extend(bytes(cap - len(self._buf)))
        first = min(len(view), cap - start)
        self._buf[start : start + first] = view[:first]
        if first < len(view):
            self._buf[: len(view) - first] = view[first:]
        return offset

    def read(self, offset: int, size: int | None = None) -> bytes:
        """Return the bytes from ``offset`` (up to ``size`` of them)."""
        if offset < self.start_offset or offset > self._end:
            raise RingOffsetError(
                f"offset {offset} outside [{self.start_offset}, {self._end}]"
            )
        stop = self._end if size is None else min(self._end, offset + size)
        length = stop - offset
        if length <= 0:
            return b""
        start = offset % self.capacity
        first = min(length, self.capacity - start)
        return bytes(self._buf[start : start + first]) + bytes(
            self._buf[: length - first]
        )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class _Viewer:
    __slots__ = ("on_close", "on_output", "watching")

    def __init__(
        self,
        on_output: OutputListener,
        watching: Callable[[], bool],
        on_close: Callable[[], None] | None,
    ):
        self.on_output = on_output
        self.watching = watching
        self.on_close = on_close


class TerminalSession:
    """A PTY child, its output history and its headless screen."""

    kind = "shell"

    def __init__(
        self,
        command: str,
        *,
        worktree_path: str = "",
        agent_id: str = "",
        rows: int = 24,
        cols: int = 80,
        ring_capacity: int = DEFAULT_RING_CAPACITY,
        spawn_id: str = "",
    ) -> None:
        self.command = command
        self.worktree_path = worktree_path
        self.agent_id = agent_id
        # Engine-wide handle, assigned by whoever registers the session.
        self.session_id = ""
        self.rows = rows
        self.cols = cols
        # Diagnostics only: joins the keypress that spawned this to its output.
        self.spawn_id = spawn_id

        self.screen = ScrollbackScreen(cols, rows)
        self._stream = pyte.Stream(self.screen)
        self.ring = ByteRing(ring_capacity)
        # Decodes each chunk as it arrives (observers, terminal modes).
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Decodes the backlog as it is fed to the screen, later; a sequence
        # split across two feeds waits in it.
        self._feed_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # The ring offset of the first byte not yet handed to _feed_decoder.
        self._fed_offset = 0

        self.mouse_tracking = False
        # DECSET 2004 — set once the child asks for bracketed paste. Until
        # then we must not send paste markers; a child that never requested
        # them would take the escape sequence for literal input.
        self.bracketed_paste = False
        # Set on every output chunk; send_input waits on it to know the child
        # has read what we wrote. See SUBMIT_ACK_TIMEOUT.
        self._output_event = asyncio.Event()

        self.exit_code: int | None = None
        self._emulator: PtyEmulator | None = None
        self._recv_task: asyncio.Task | None = None
        self._stopped = False

        self._listeners: list[SessionListener] = []
        self._viewers: list[_Viewer] = []
        self._size_listeners: list[SizeListener] = []
        # Raw output not yet fed to the screen, oldest first, and its size.
        self._pending_feed: deque[bytes] = deque()
        self._pending_chars = 0
        self._feed_handle: asyncio.TimerHandle | None = None
        # Characters dropped unparsed because the feeder was too far behind.
        self.feed_dropped = 0
        # The feeder this session is queued on, if any.
        self._queued_on: _Feeder | None = None

        self._diag_started = 0.0
        self._diag_chunks = 0
        self._diag_bytes = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True while the child's PTY is open."""
        return self._emulator is not None

    @property
    def pid(self) -> int | None:
        return self._emulator.pid if self._emulator is not None else None

    def start(self) -> None:
        """Spawn the child and start reading its output."""
        if self._emulator is not None:
            return
        self._stopped = False
        self._diag_started = time.perf_counter()
        with diag.timed(
            log,
            "term.start",
            session=self._diag_name,
            spawn_id=self.spawn_id,
        ) as span:
            self._emulator = PtyEmulator(command=self.command)
            self._emulator.start()
            span["pid"] = self._emulator.pid
        self._recv_task = asyncio.create_task(self._recv())

    def stop(self) -> None:
        """Kill the child. No exit event is emitted for an explicit stop."""
        if self._emulator is None:
            return
        self._stopped = True
        self._close()

    def _close(self) -> None:
        emulator = self._emulator
        if emulator is None:
            return
        diag.event(
            log,
            "term.stop",
            session=self._diag_name,
            spawn_id=self.spawn_id,
            chunks=self._diag_chunks,
            bytes=self._diag_bytes,
        )
        self._emulator = None
        if self._recv_task is not None and self._recv_task is not _current_task():
            self._recv_task.cancel()
        self._recv_task = None
        emulator.stop()
        if emulator.returncode is not None:
            self.exit_code = emulator.returncode
        self._cancel_feed_timer()
        self._unqueue()
        self._before_close()
        for viewer in list(self._viewers):
            if viewer.on_close is not None:
                try:
                    viewer.on_close()
                except Exception:
                    log.exception("viewer close callback failed")

    async def _recv(self) -> None:
        emulator = self._emulator
        assert emulator is not None
        try:
            while True:
                message = await emulator.send_queue.get()
                cmd = message[0]
                if cmd == "setup":
                    emulator.recv_queue.put_nowait(["set_size", self.rows, self.cols])
                elif cmd == "stdout":
                    self._on_chunk(message[1])
                elif cmd == "disconnect":
                    break
            if self._stopped:
                return
            self.flush()
            self._on_disconnect()
            self.exit_code = await emulator.wait_exit(_EXIT_REAP_TIMEOUT)
        except asyncio.CancelledError:
            return
        if self._stopped:
            return
        self._close()
        self._emit(SessionExited(self.worktree_path, self.agent_id, self.exit_code))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _on_chunk(self, data: bytes) -> None:
        """Handle one chunk of raw output from the child."""
        offset = self.ring.append(data)
        text = self._decoder.decode(data)

        if diag.ENABLED:
            self._diag_chunks += 1
            self._diag_bytes += len(data)
            if self._diag_chunks == 1:
                # Closes the Ctrl+J chain: keypress -> spawn -> fork -> the
                # child actually saying something.
                diag.event(
                    log,
                    "term.first_output",
                    session=self._diag_name,
                    spawn_id=self.spawn_id,
                    bytes=len(data),
                    since_start_ms=round(
                        (time.perf_counter() - self._diag_started) * 1000, 2
                    ),
                )

        self._on_text(text)
        if text:
            self._scan_terminal_modes(text)
        if data:
            self._pending_feed.append(data)
            self._pending_chars += len(data)
        self._output_event.set()

        if any(viewer.watching() for viewer in self._viewers):
            self.flush()
        elif self._pending_feed:
            self._bound_pending()
            if self._feed_handle is None and self._queued_on is None:
                self._feed_handle = asyncio.get_running_loop().call_later(
                    UNWATCHED_FEED_INTERVAL, self._feed_later
                )

        for viewer in list(self._viewers):
            viewer.on_output(offset, data)

    def _feed_later(self) -> None:
        """The batching interval is over: let the loop's feeder parse it."""
        self._feed_handle = None
        if self._pending_feed:
            _feeder().add(self)

    def _unqueue(self) -> None:
        if self._queued_on is not None:
            self._queued_on.discard(self)

    @property
    def parsed_offset(self) -> int:
        """The output offset the screen is exact at: every byte before it is
        in the screen (or was dropped unparsed, see :meth:`bound_pending`),
        none from it on."""
        return self._fed_offset - len(self._feed_decoder.getstate()[0])

    def _feed(self, data: bytes) -> None:
        self._fed_offset += len(data)
        text = self._feed_decoder.decode(data)
        if not text:
            return
        try:
            self._stream.feed(text)
        except TypeError as error:
            log.warning("could not feed: %s", error)

    def _bound_pending(self) -> None:
        self.bound_pending(PENDING_FEED_LIMIT, PENDING_FEED_KEEP)

    def bound_pending(self, limit: int, keep: int | None = None) -> None:
        """Keep at most ``keep`` (default ``limit``) bytes of a backlog that
        passed ``limit``, starting at a line.

        What is dropped never reaches the screen or the scrollback; a
        full-screen program's next redraw repairs whatever the cut left.
        """
        if self._pending_chars <= limit:
            return
        keep = limit if keep is None else keep
        data = b"".join(self._pending_feed)
        cut = len(data) - keep
        newline = data.find(b"\n", cut)
        if newline != -1:
            cut = newline + 1
        kept = data[cut:]
        self.feed_dropped += cut
        log.info(
            "%s: %d bytes of output left unparsed (too far behind)",
            self._diag_name,
            cut,
        )
        # The screen skips them: it is exact at the cut from now on.
        self._fed_offset += cut
        self._feed_decoder.reset()
        self._pending_feed = deque([kept]) if kept else deque()
        self._pending_chars = len(kept)

    def _feed_slice(self, limit: int) -> bool:
        """Feed up to ``limit`` batched bytes; False once nothing is left."""
        pending = self._pending_feed
        if not pending:
            return False
        piece = pending[0]
        if len(piece) <= limit:
            pending.popleft()
        else:
            pending[0] = piece[limit:]
            piece = piece[:limit]
        self._pending_chars -= len(piece)
        self._feed(piece)
        if not pending:
            self._after_feed()
        return bool(pending)

    def flush(self) -> None:
        """Feed any batched output into the screen now.

        Idempotent. Viewers call it before reading the screen after a spell
        of not watching, and before a resize, so batched output is applied at
        the size it was produced for.
        """
        self._cancel_feed_timer()
        self._unqueue()
        if not self._pending_feed:
            return
        data = b"".join(self._pending_feed)
        batched = len(self._pending_feed)
        self._pending_feed.clear()
        self._pending_chars = 0
        feed_start = time.perf_counter() if diag.HOT else 0.0
        self._feed(data)
        if diag.HOT:
            diag.debug_event(
                log,
                "term.feed",
                session=self._diag_name,
                chunks=batched,
                chars=len(data),
                feed_ms=round((time.perf_counter() - feed_start) * 1000, 3),
            )
        self._after_feed()

    async def catch_up(self) -> None:
        """Feed the backlog in the feeder's slices, yielding to the loop
        between them, until the screen has all the output there was when
        called (for a reader that wants the latest screen and may wait for
        it, without stalling the loop). Output that arrives meanwhile is
        left to the feeder, so a chatty child cannot keep this going."""
        clock = time.perf_counter
        target = self.ring.end_offset
        while self._pending_feed and self._fed_offset < target:
            deadline = clock() + FEED_BUDGET
            while self._fed_offset < target and clock() < deadline:
                if not self._feed_slice(min(FEED_SLICE, target - self._fed_offset)):
                    break
            await asyncio.sleep(0)

    def _cancel_feed_timer(self) -> None:
        if self._feed_handle is not None:
            self._feed_handle.cancel()
            self._feed_handle = None

    def _scan_terminal_modes(self, chars: str) -> None:
        """Track the DECSET private modes we care about in an output chunk."""
        self.mouse_tracking, self.bracketed_paste = track_modes(
            chars, self.mouse_tracking, self.bracketed_paste
        )

    # ------------------------------------------------------------------
    # Subscribers
    # ------------------------------------------------------------------

    def add_listener(self, listener: SessionListener) -> Callable[[], None]:
        """Call ``listener`` with every status/exit event; returns a remover."""
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def attach(
        self,
        on_output: OutputListener,
        *,
        watching: Callable[[], bool] = lambda: True,
        on_close: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        """Call ``on_output(offset, data)`` for every output chunk, and
        ``on_close()`` once the PTY is gone (stopped or exited).

        While any viewer's ``watching()`` is true the screen is fed before
        the call, so the viewer can render it right away. Returns a detach
        function.
        """
        viewer = _Viewer(on_output, watching, on_close)
        self._viewers.append(viewer)

        def detach() -> None:
            if viewer in self._viewers:
                self._viewers.remove(viewer)

        return detach

    def on_resize(self, listener: SizeListener) -> Callable[[], None]:
        """Call ``listener(rows, cols, offset)`` after every size change;
        returns a remover."""
        self._size_listeners.append(listener)

        def remove() -> None:
            if listener in self._size_listeners:
                self._size_listeners.remove(listener)

        return remove

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    def snapshot(
        self, max_scrollback: int = SNAPSHOT_SCROLLBACK_LINES, *, flush: bool = True
    ) -> tuple[int, str]:
        """``(offset, ansi)``: the screen as ANSI (see
        :func:`~lazyagent.engine.screen.ansi_snapshot`) and the output offset
        it is exact at. The bytes from ``offset`` on are not in it: a client
        that renders the snapshot then feeds the output from ``offset`` ends
        up with this screen.

        ``flush=False`` takes the screen as parsed so far, without feeding
        the backlog: ``offset`` is then :attr:`parsed_offset`, and the
        backlog is what the ring holds from it.
        """
        if flush:
            self.flush()
        # A chunk that ended inside a UTF-8 sequence leaves its first bytes
        # in the decoder, out of the screen.
        return self.parsed_offset, ansi_snapshot(self.screen, max_scrollback)

    def _emit(self, event: SessionEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("session listener failed")

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def write(self, data: bytes | str) -> None:
        """Queue raw input for the child (keys, mouse reports, commands)."""
        if self._emulator is None:
            raise RuntimeError("terminal is not running")
        self._emulator.recv_queue.put_nowait(["stdin", data])

    def resize(self, rows: int, cols: int) -> None:
        """Resize the PTY and the headless screen together."""
        if rows <= 0 or cols <= 0 or (rows, cols) == (self.rows, self.cols):
            return
        self.flush()
        self.rows = rows
        self.cols = cols
        self.screen.resize(rows, cols)
        if self._emulator is not None:
            self._emulator.recv_queue.put_nowait(["set_size", rows, cols])
        offset = self.ring.end_offset
        for listener in list(self._size_listeners):
            try:
                listener(rows, cols, offset)
            except Exception:
                log.exception("size listener failed")

    def frame_input(self, text: str) -> str:
        """Frame ``text`` as a paste, per the child's current modes (see
        :func:`frame_paste`)."""
        return frame_paste(text, self.bracketed_paste)

    async def send_input(self, text: str, *, submit: bool = False) -> None:
        """Write ``text`` into the child, optionally pressing Enter after it.

        The payload and the submit key are always separate writes, and the
        submit key is held back until the child has acknowledged the payload
        (see ``_await_child_ack``). Two writes alone would not be enough — the
        tty input buffer coalesces back-to-back writes into a single read on
        the child side.
        """
        if self._emulator is None:
            raise RuntimeError("terminal is not running")

        # Cleared before the write so only output produced *after* it counts
        # as an acknowledgement.
        self._output_event.clear()
        self.write(self.frame_input(text))

        if not submit:
            return

        await self._await_child_ack()
        self.write(SUBMIT_KEY)

    async def _await_child_ack(self) -> None:
        """Wait (briefly, and bounded) for the child to react to our write."""
        try:
            await asyncio.wait_for(self._output_event.wait(), SUBMIT_ACK_TIMEOUT)
        except asyncio.TimeoutError:
            # Silent child — submit anyway rather than drop the instruction.
            pass
        if SUBMIT_SETTLE_DELAY > 0:
            await asyncio.sleep(SUBMIT_SETTLE_DELAY)

    # ------------------------------------------------------------------
    # Screen text
    # ------------------------------------------------------------------

    @property
    def total_lines(self) -> int:
        """Scrollback plus live screen rows."""
        return len(self.screen.scrollback) + self.screen.lines

    def tail_lines(self, n: int) -> list[str]:
        """The last ``n`` lines of scrollback + screen, oldest first."""
        self.flush()
        screen = self.screen
        collected: list[str] = []
        remaining = max(0, min(n, self.total_lines))
        for row in range(screen.lines - 1, -1, -1):
            if remaining <= 0:
                break
            collected.append(screen.screen_text(row))
            remaining -= 1
        for index in range(len(screen.scrollback) - 1, -1, -1):
            if remaining <= 0:
                break
            collected.append(screen.scrollback_text(index))
            remaining -= 1
        collected.reverse()
        return collected

    def screen_text(self) -> str:
        """The live screen as text, one line per row (trailing blanks kept)."""
        screen = self.screen
        lines: list[str] = []
        for y in range(screen.lines):
            row = screen.buffer[y]
            lines.append("".join(row[x].data for x in range(screen.columns)))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Hooks
    # ------------------------------------------------------------------

    @property
    def _diag_name(self) -> str:
        return f"{self.kind}:{self.worktree_path}:{self.agent_id}"

    def _on_text(self, text: str) -> None:
        """Decoded output, per chunk, before it is fed to the screen.

        ``text`` is empty when the chunk ended inside a UTF-8 sequence.
        """

    def _after_feed(self) -> None:
        """The screen has just been fed."""

    def _on_disconnect(self) -> None:
        """The child closed the PTY (not an explicit stop)."""

    def _before_close(self) -> None:
        """The PTY is being torn down, for whatever reason."""


class AgentSession(TerminalSession):
    """A coding agent's session: a terminal session plus status detection."""

    kind = "agent"

    def __init__(
        self,
        command: str,
        *,
        worktree_path: str,
        agent_id: str = "",
        observer: AgentObserver | None = None,
        **kwargs,
    ) -> None:
        super().__init__(
            command, worktree_path=worktree_path, agent_id=agent_id, **kwargs
        )
        self.status = AgentStatus.NO_AGENT
        self.confidence = LifecycleConfidence.LOW
        self.detail = ""
        self.last_output_time: float | None = None  # time.monotonic()
        self._observer = observer or AgentObserver()
        self._scan_timer: asyncio.TimerHandle | None = None
        # Predicted dispatch time of the latest scheduled scan; used in
        # _after_feed to enforce _SCAN_MIN_INTERVAL.
        self._next_scan_dispatch: float = 0.0

    @property
    def conversation_id(self) -> str | None:
        """The provider's conversation id, once its observer saw one."""
        return self._observer.conversation_id

    @property
    def first_prompt(self) -> str | None:
        """The first prompt the agent got, once its observer saw it."""
        return self._observer.first_prompt

    def start(self) -> None:
        """Spawn the agent and report it RUNNING."""
        if self.running:
            return
        super().start()
        self._set_status(AgentStatus.RUNNING)
        self._apply_events(self._observer.on_process_started())

    def stop(self) -> None:
        """Kill the agent and drop its observer's files (hook logs, settings).

        No exit event, as for any explicit stop. Also cleans up a session
        that was never started, so an abandoned spawn leaves nothing behind.
        """
        super().stop()
        self._observer.cleanup()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _set_status(
        self,
        new_status: AgentStatus,
        confidence: LifecycleConfidence = LifecycleConfidence.LOW,
        detail: str = "",
    ) -> None:
        if new_status == self.status and detail == self.detail:
            return
        diag.event(
            log,
            "agent.status",
            agent_id=self.agent_id,
            worktree=self.worktree_path,
            status=new_status.value,
            previous=self.status.value,
            confidence=confidence.value,
        )
        self.status = new_status
        self.confidence = confidence
        self.detail = detail
        if not self._stopped:
            self._emit(
                StatusChanged(
                    self.worktree_path,
                    self.agent_id,
                    new_status,
                    confidence,
                    detail,
                    self.last_output_time,
                )
            )

    def _apply_events(self, events: list[AgentLifecycleEvent]) -> None:
        for event in events:
            self._set_status(
                event.status, confidence=event.confidence, detail=event.detail
            )

    def _on_text(self, text: str) -> None:
        self.last_output_time = time.monotonic()
        # First output transitions from NO_AGENT to RUNNING.
        if self.status == AgentStatus.NO_AGENT:
            self._set_status(AgentStatus.RUNNING)
        self._apply_events(self._observer.on_terminal_output(text))

    def poll(self) -> None:
        """Apply whatever the agent's hooks/telemetry reported since last time."""
        self._apply_events(self._observer.poll())

    def check_hang(self) -> None:
        """Report POSSIBLY_HANGED when RUNNING with no output for too long."""
        self.poll()
        if (
            self.status == AgentStatus.RUNNING
            and self.last_output_time is not None
            and time.monotonic() - self.last_output_time >= HANG_SECONDS
        ):
            self._set_status(AgentStatus.POSSIBLY_HANGED)

    # ------------------------------------------------------------------
    # Screen scan
    # ------------------------------------------------------------------

    def _after_feed(self) -> None:
        """Schedule a debounced screen scan, throttled to _SCAN_MIN_INTERVAL.

        Without the floor, bursty output causes scans to fire every ~150 ms
        whenever the stream briefly pauses. Capping the dispatch rate keeps
        the full-screen text join from running more than ~2 Hz regardless
        of output cadence.
        """
        if self._scan_timer is not None:
            self._scan_timer.cancel()
        loop = asyncio.get_running_loop()
        now = loop.time()
        delay = max(_SCAN_DEBOUNCE_SECONDS, self._next_scan_dispatch - now)
        self._next_scan_dispatch = now + delay + _SCAN_MIN_INTERVAL
        self._scan_timer = loop.call_later(delay, self.scan_screen)

    def scan_screen(self) -> None:
        """Let the observer read the headless screen, then poll it."""
        # Runs on the event loop, once per agent, up to ~2 Hz — so with several
        # agents its cost lands squarely on the main thread.
        if not diag.HOT:
            self._do_scan_screen()
            return
        start = time.perf_counter()
        try:
            self._do_scan_screen()
        finally:
            diag.debug_event(
                log,
                "agent.scan_screen",
                agent_id=self.agent_id,
                worktree=self.worktree_path,
                duration_ms=round((time.perf_counter() - start) * 1000, 3),
            )

    def _do_scan_screen(self) -> None:
        # Rendering the screen to text is the scan's cost; only an observer
        # that reads it pays for it (proposal 0005). The poll stays: output
        # is often what a hook log entry follows.
        if self._observer.needs_screen:
            self._apply_events(
                self._observer.on_screen_update(
                    self.screen_text(),
                    current_status=self.status,
                    current_detail=self.detail,
                )
            )
        self.poll()

    def _on_disconnect(self) -> None:
        self._apply_events(self._observer.on_disconnect())
        self.poll()

    def _before_close(self) -> None:
        if self._scan_timer is not None:
            self._scan_timer.cancel()
            self._scan_timer = None
        if not self._stopped:
            # Exiting on its own: the observer has had its last word.
            self._observer.cleanup()
            self.status = AgentStatus.NO_AGENT


def track_modes(
    chars: str, mouse_tracking: bool, bracketed_paste: bool
) -> tuple[bool, bool]:
    """``(mouse_tracking, bracketed_paste)`` after the DECSET private modes
    an output chunk sets.

    Mouse tracking (1000) decides whether clicks/scroll go to the child;
    bracketed paste (2004) decides how input is framed as a paste.
    """
    for sep_match in re.finditer(RE_ANSI_SEQUENCE, chars):
        sequence = sep_match.group(0)
        if not sequence.startswith(DECSET_PREFIX):
            continue
        body = sequence.removeprefix(DECSET_PREFIX)
        action = body[-1:]
        if action not in ("h", "l"):
            continue
        enabled = action == "h"
        # Only the last parameter carries the final byte: "1000;1006h"
        # → ["1000", "1006"].
        modes = body[:-1].split(";")
        if "1000" in modes:
            mouse_tracking = enabled
        if "2004" in modes:
            bracketed_paste = enabled
    return mouse_tracking, bracketed_paste


def frame_paste(text: str, bracketed_paste: bool) -> str:
    """Frame ``text`` the way a terminal delivers content, not keystrokes.

    Wrapped in bracketed-paste markers when the child has enabled DECSET
    2004, which is what stops an embedded control character (in practice
    the submit CR that follows) from being swallowed into the same text
    run.
    """
    # Content newlines are LF. A CR left in the body reads as Enter to any
    # child that does parse it as a key, which would submit half a message.
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    if not bracketed_paste:
        return body
    # A literal end marker inside the payload would close the paste early
    # and let the remainder be parsed as keypresses.
    body = body.replace(BRACKETED_PASTE_END, "")
    return BRACKETED_PASTE_START + body + BRACKETED_PASTE_END


class _Feeder:
    """Parses hidden sessions' batched output in slices of a few
    milliseconds, yielding to the loop between them and resting after a full
    slice, so parsing takes at most about half the loop's time.

    Sessions somebody is attached to go first (their screen may be read for
    a snapshot); the others take turns. Status detection reads the screen a
    little later, which it tolerates.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        # An ordered set: dict keys keep insertion order.
        self._queue: dict[TerminalSession, None] = {}
        self._task: asyncio.Task | None = None

    def add(self, session: TerminalSession) -> None:
        self._queue[session] = None
        session._queued_on = self
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._run())

    def discard(self, session: TerminalSession) -> None:
        self._queue.pop(session, None)
        session._queued_on = None

    async def _run(self) -> None:
        clock = time.perf_counter
        while self._queue:
            deadline = clock() + FEED_BUDGET
            order = sorted(self._queue, key=lambda s: s.viewer_count == 0)
            for session in order:
                if session not in self._queue:
                    continue
                more = True
                while more and clock() < deadline:
                    more = session._feed_slice(FEED_SLICE)
                # Take turns: whoever was fed goes to the back.
                self.discard(session)
                if more:
                    self.add(session)
                if clock() >= deadline:
                    break
            await asyncio.sleep(FEED_REST if clock() >= deadline else 0)


_feeders: dict[asyncio.AbstractEventLoop, _Feeder] = {}


def _feeder() -> _Feeder:
    """The running loop's feeder."""
    loop = asyncio.get_running_loop()
    feeder = _feeders.get(loop)
    if feeder is None:
        for old in [lp for lp in _feeders if lp.is_closed()]:
            del _feeders[old]
        feeder = _feeders[loop] = _Feeder(loop)
    return feeder


def _current_task() -> asyncio.Task | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
