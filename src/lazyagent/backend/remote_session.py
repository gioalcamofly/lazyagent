"""A terminal of a remote engine, as the TUI's views see it.

:class:`RemoteSession` stands in for the engine's
:class:`~lazyagent.engine.session.TerminalSession` on the client side. It
keeps a display screen of its own (a
:class:`~lazyagent.engine.screen.ScrollbackScreen`, so the views render it
exactly as they render an engine session) and feeds it from the session's
v1 stream:

- The stream is attached only while a view is attached (a view attaches
  while it is shown): hidden terminals cost nothing (K2). The first attach
  renders the engine's ANSI snapshot; a later one asks for the raw bytes
  from the last offset it fed (``from_offset``), which the engine answers
  with that tail when it still has it, else with a snapshot again.
- Every output frame carries its offset. A frame past the last fed byte
  (a gap) or a ``stream.reset`` re-attaches from the last fed byte.
- The screen has the PTY's size, not the view's: ``stream.resized`` resizes
  it where it falls in the byte stream, and the view crops or pads (§3c).
  :meth:`resize` asks the engine for a size (``claim_size``, which also
  makes this client the size owner); the screen follows when the engine
  says the PTY changed.
- Keys and pastes go out as binary input frames on the stream.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import time
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

# isort: off
import lazyagent.pyte_patch  # noqa: F401
import pyte
# isort: on

from lazyagent.engine.client import ConnectionClosed, RequestError
from lazyagent.engine.screen import ScrollbackScreen
from lazyagent.engine.session import SUBMIT_KEY, frame_paste, track_modes
from lazyagent.models import AgentStatus

if TYPE_CHECKING:
    from lazyagent.backend.remote import RemoteBackend
    from lazyagent.protocol.envelope import Event

log = logging.getLogger(__name__)

OutputListener = Callable[[int, bytes], None]

# Pause between a paste and the Enter that submits it, so the child reads
# them apart (the engine's send_input waits for an echo instead).
_SUBMIT_DELAY = 0.05
# Keys typed while the stream is (re)attaching are kept, up to this much.
_INPUT_BACKLOG_LIMIT = 64 * 1024


class _Viewer:
    __slots__ = ("on_close", "on_output")

    def __init__(
        self, on_output: OutputListener, on_close: Callable[[], None] | None
    ) -> None:
        self.on_output = on_output
        self.on_close = on_close


class RemoteSession:
    """One remote PTY session: its display screen and its stream."""

    def __init__(
        self,
        backend: RemoteBackend,
        session_id: str,
        *,
        kind: str,
        worktree_path: str,
        agent_id: str = "",
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        self._backend = backend
        self.session_id = session_id
        self.kind = kind
        self.worktree_path = worktree_path
        self.agent_id = agent_id
        self.spawn_id = ""
        self.rows = rows
        self.cols = cols
        self.screen = ScrollbackScreen(cols, rows)
        self._stream = pyte.Stream(self.screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.mouse_tracking = False
        self.bracketed_paste = False
        self.running = True
        self.exit_code: int | None = None
        self.last_output_time: float | None = None

        # Offset after the last output byte fed to the screen; None until a
        # snapshot or tail has been rendered.
        self.received: int | None = None
        self.stream_id: int | None = None
        # Bumped whenever the stream in use is abandoned, so frames of an old
        # stream still in flight are ignored.
        self._generation = 0
        self._ready_generation = -1
        # Frames and stream events that arrived before their attach reply was
        # applied (the client reads them in the same chunk as the reply).
        self._early: list[tuple[str, Any]] = []
        self._viewers: list[_Viewer] = []
        self._task: asyncio.Task | None = None
        self._input_backlog: list[bytes] = []
        self._requested_size: tuple[int, int] | None = None
        # Diagnostics and tests: how each attach was answered, gaps seen.
        self.attaches: list[str] = []
        self.gaps = 0

    # ------------------------------------------------------------------
    # What the views read
    # ------------------------------------------------------------------

    @property
    def status(self) -> AgentStatus:
        state = self._backend.registry.agents(self.worktree_path).get(self.agent_id)
        return state.status if state is not None else AgentStatus.NO_AGENT

    @property
    def attached(self) -> bool:
        return self.stream_id is not None

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    def attach(
        self,
        on_output: OutputListener,
        *,
        watching: Callable[[], bool] = lambda: True,
        on_close: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        """Call ``on_output(offset, data)`` after every chunk is fed to the
        screen (``data`` is empty for a repaint without new output, such as
        a snapshot or a resize), and ``on_close()`` once the session is gone.

        The stream is attached while at least one viewer is. ``watching`` is
        accepted for parity with the engine session: this screen is always
        fed as frames arrive.
        """
        viewer = _Viewer(on_output, on_close)
        self._viewers.append(viewer)
        self._reconcile()

        def detach() -> None:
            if viewer in self._viewers:
                self._viewers.remove(viewer)
                self._reconcile()

        return detach

    def flush(self) -> None:
        """Nothing is batched: frames are fed as they arrive."""

    def start(self) -> None:
        """The engine runs the process."""

    def stop(self) -> None:
        """The engine stops the process (``stop_agent``)."""

    # ------------------------------------------------------------------
    # Input and size
    # ------------------------------------------------------------------

    def write(self, data: bytes | str) -> None:
        """Send raw input (keys, mouse reports, pastes) to the child."""
        if not self.running:
            raise RuntimeError("terminal is not running")
        if isinstance(data, str):
            data = data.encode()
        client = self._backend.client
        if (
            client is None
            or not self.attached
            or self._ready_generation != self._generation
        ):
            if sum(map(len, self._input_backlog)) + len(data) <= _INPUT_BACKLOG_LIMIT:
                self._input_backlog.append(data)
            self._reconcile()
            return
        try:
            client.send_input(self.stream_id, data)
        except (ConnectionError, OSError, RuntimeError) as e:
            log.info("input to %s lost: %s", self.session_id, e)

    def resize(self, rows: int, cols: int) -> None:
        """Ask for the PTY to be ``rows`` x ``cols`` and to own its size; the
        screen follows once the engine reports the change."""
        if rows <= 0 or cols <= 0:
            return
        target = (rows, cols)
        if self._requested_size == target:
            return
        if self._requested_size is None and target == (self.rows, self.cols):
            return
        self._requested_size = target

        def done(result: Any) -> None:
            if self._requested_size == target:
                self._requested_size = None

        self._backend.fire(
            "claim_size",
            on_done=done,
            session_id=self.session_id,
            rows=rows,
            cols=cols,
        )

    def frame_input(self, text: str) -> str:
        return frame_paste(text, self.bracketed_paste)

    async def send_input(self, text: str, *, submit: bool = False) -> None:
        self.write(self.frame_input(text))
        if submit:
            await asyncio.sleep(_SUBMIT_DELAY)
            self.write(SUBMIT_KEY)

    # ------------------------------------------------------------------
    # Text, as the engine session offers it
    # ------------------------------------------------------------------

    @property
    def total_lines(self) -> int:
        return len(self.screen.scrollback) + self.screen.lines

    def screen_text(self) -> str:
        screen = self.screen
        return "\n".join(
            "".join(screen.buffer[y][x].data for x in range(screen.columns))
            for y in range(screen.lines)
        )

    # ------------------------------------------------------------------
    # The stream
    # ------------------------------------------------------------------

    def _reconcile(self) -> None:
        """Attach or detach the stream to match the viewers, in the
        background (one task at a time)."""
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._task = loop.create_task(self._run())

    def _wanted(self) -> bool:
        return bool(self._viewers) and self.running and self._backend.client is not None

    async def _run(self) -> None:
        while True:
            want = self._wanted()
            if want and not self.attached:
                if not await self._attach():
                    return
            elif not want and self.attached:
                await self._detach()
            else:
                return

    async def _attach(self) -> bool:
        client = self._backend.client
        if client is None:
            return False
        self._generation += 1
        generation = self._generation
        self._early.clear()
        try:
            stream = await client.attach(
                self.session_id,
                partial(self._on_output, generation),
                from_offset=self.received,
                on_event=partial(self._on_stream_event, generation),
            )
        except RequestError as e:
            log.info("attach to %s refused: %s", self.session_id, e)
            self._close(None)
            return False
        except (ConnectionClosed, ConnectionError, OSError):
            return False
        if generation != self._generation:
            # Abandoned while attaching; the loop decides again.
            self._backend.fire("detach_stream", stream_id=stream.stream_id)
            return True
        self.stream_id = stream.stream_id
        self.attaches.append(stream.mode)
        if stream.mode == "snapshot" or self.received is None:
            self._reset_screen(stream.rows, stream.cols)
            self._feed_text(stream.snapshot or "")
        elif (stream.rows, stream.cols) != (self.rows, self.cols):
            self._resize_screen(stream.rows, stream.cols)
        self.received = stream.offset
        self._ready_generation = generation
        early, self._early = self._early, []
        for kind, item in early:
            if generation != self._generation:
                break
            if kind == "output":
                self._feed(*item)
            else:
                self._apply_stream_event(item)
        self._notify(self.received, b"")
        backlog, self._input_backlog = self._input_backlog, []
        for data in backlog:
            self.write(data)
        return True

    async def _detach(self) -> None:
        stream_id, self.stream_id = self.stream_id, None
        self._generation += 1
        client = self._backend.client
        if client is None or stream_id is None:
            return
        try:
            await client.detach(stream_id)
        except (RequestError, ConnectionClosed, ConnectionError, OSError):
            pass

    def _abandon_stream(self) -> None:
        """Stop using the current stream (gap, reset); the next attach
        resumes from :attr:`received`."""
        stream_id, self.stream_id = self.stream_id, None
        self._generation += 1
        client = self._backend.client
        if stream_id is not None and client is not None and client.stream(stream_id):
            self._backend.fire("detach_stream", stream_id=stream_id)
        self._reconcile()

    def _on_output(self, generation: int, offset: int, data: bytes) -> None:
        if generation != self._generation:
            return
        if self._ready_generation != generation:
            self._early.append(("output", (offset, data)))
            return
        self._feed(offset, data)

    def _on_stream_event(self, generation: int, event: Event) -> None:
        if generation != self._generation:
            return
        if self._ready_generation != generation:
            self._early.append(("event", event))
            return
        self._apply_stream_event(event)

    def _feed(self, offset: int, data: bytes) -> None:
        received = self.received if self.received is not None else offset
        if offset > received:
            self.gaps += 1
            log.info(
                "stream of %s skipped %d bytes; re-attaching",
                self.session_id,
                offset - received,
            )
            self._abandon_stream()
            return
        if offset < received:
            data = data[received - offset :]
            offset = received
            if not data:
                return
        self.received = offset + len(data)
        self.last_output_time = time.monotonic()
        self._feed_text(self._decoder.decode(data))
        self._notify(offset, data)

    def _feed_text(self, text: str) -> None:
        if not text:
            return
        self.mouse_tracking, self.bracketed_paste = track_modes(
            text, self.mouse_tracking, self.bracketed_paste
        )
        try:
            self._stream.feed(text)
        except TypeError as error:
            log.warning("could not feed: %s", error)

    def _apply_stream_event(self, event: Event) -> None:
        data = event.data
        if event.event == "stream.resized":
            self._resize_screen(data["rows"], data["cols"])
            self._notify(self.received or 0, b"")
        elif event.event == "stream.reset":
            self.stream_id = None
            self._generation += 1
            self._reconcile()
        elif event.event == "stream.closed":
            self.stream_id = None
            self._generation += 1
            self._close(data.get("exit_code"))

    def _reset_screen(self, rows: int, cols: int) -> None:
        """Empty the screen (scrollback too) at the PTY's size, in place:
        the views hold on to this screen object."""
        self.rows, self.cols = rows, cols
        self.screen.scrollback.clear()
        if (self.screen.lines, self.screen.columns) != (rows, cols):
            self.screen.resize(rows, cols)
        self.screen.reset()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.mouse_tracking = self.bracketed_paste = False

    def _resize_screen(self, rows: int, cols: int) -> None:
        if (rows, cols) == (self.rows, self.cols):
            return
        self.rows, self.cols = rows, cols
        self.screen.resize(rows, cols)

    def _notify(self, offset: int, data: bytes) -> None:
        for viewer in list(self._viewers):
            try:
                viewer.on_output(offset, data)
            except Exception:
                log.exception("viewer failed")

    def _close(self, exit_code: int | None) -> None:
        if not self.running:
            return
        self.running = False
        self.exit_code = exit_code
        self._input_backlog.clear()
        for viewer in list(self._viewers):
            if viewer.on_close is not None:
                try:
                    viewer.on_close()
                except Exception:
                    log.exception("viewer close callback failed")

    # ------------------------------------------------------------------
    # The connection (called by the backend)
    # ------------------------------------------------------------------

    def connection_lost(self) -> None:
        """The stream died with the connection; keep the screen as it is."""
        self.stream_id = None
        self._generation += 1
        self._requested_size = None

    def connection_restored(self) -> None:
        """Same engine again: resume the stream from the last fed byte."""
        self._reconcile()

    def gone(self) -> None:
        """The engine no longer runs this session."""
        self.stream_id = None
        self._generation += 1
        self._close(self.exit_code)
