"""Shared steps of the contract tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pyte

from lazyagent.engine.client import Client, Stream
from lazyagent.engine.screen import ScrollbackScreen
from lazyagent.protocol import validation


async def until(
    predicate, timeout: float = 10.0, interval: float = 0.02, what: str = ""
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(
                f"{what or 'condition'} not met within {timeout:g}s"
            )
        await asyncio.sleep(interval)


async def checked(client: Client, method: str, **params: Any) -> Any:
    """Call ``method`` with params and result both validated strictly."""
    validation.validate_params(method, params, strict=True)
    result = await client.request(method, **params)
    validation.validate_result(method, result, strict=True)
    return result


async def spawn(client: Client, worktree_path: str, *args: str) -> dict:
    """Spawn the fake agent (``args`` are its arguments) and wait for its
    prompt; returns the spawn_agent result."""
    params: dict[str, Any] = {"worktree_path": worktree_path}
    if args:
        params["instruction"] = " ".join(args)
    result = await checked(client, "spawn_agent", **params)
    await wait_for_text(client, result, "your turn>")
    return result


async def screen_lines(client: Client, agent: dict, lines: int = 200) -> list[str]:
    result = await client.request(
        "read_agent_output",
        worktree_path=agent["worktree_path"],
        agent_id=agent["agent_id"],
        lines=lines,
    )
    return result["lines"]


async def wait_for_text(
    client: Client, agent: dict, text: str, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if any(text in line for line in await screen_lines(client, agent)):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"{text!r} never showed up")
        await asyncio.sleep(0.05)


class Output:
    """Collects a stream's output frames."""

    def __init__(self) -> None:
        self.frames: list[tuple[int, bytes]] = []

    def __call__(self, offset: int, data: bytes) -> None:
        self.frames.append((offset, data))

    @property
    def data(self) -> bytes:
        return b"".join(d for _, d in self.frames)

    def contiguous_from(self, start: int) -> bool:
        expected = start
        for offset, data in self.frames:
            if offset != expected:
                return False
            expected += len(data)
        return True

    def text(self) -> str:
        return self.data.decode(errors="replace")


def render(stream: Stream, output: Output) -> ScrollbackScreen:
    """What a fresh terminal shows after the attach reply and the frames."""
    screen = ScrollbackScreen(stream.cols, stream.rows)
    feed = pyte.Stream(screen)
    if stream.snapshot is not None:
        feed.feed(stream.snapshot)
    feed.feed(output.data.decode())
    return screen


def text_lines(screen: ScrollbackScreen) -> list[str]:
    """Scrollback and screen rows as text, like read_agent_output."""
    lines = [screen.scrollback_text(i) for i in range(len(screen.scrollback))]
    lines += [screen.screen_text(y) for y in range(screen.lines)]
    return lines


async def events_until(client: Client, predicate, timeout: float = 10.0) -> list:
    """Every event up to and including the first that matches."""
    seen = []

    async def collect():
        async for event in client.events():
            seen.append(event)
            if predicate(event):
                return

    await asyncio.wait_for(collect(), timeout)
    return seen
