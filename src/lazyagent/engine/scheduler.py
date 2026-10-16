"""Periodic engine work as plain asyncio tasks, started and stopped together."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Periodic:
    seconds: float
    callback: Callable[[], None]


class Scheduler:
    """Calls each registered callback every N seconds while started.

    A callback that raises is logged and keeps its schedule.
    """

    def __init__(self) -> None:
        self._periodics: list[_Periodic] = []
        self._tasks: list[asyncio.Task] = []

    def every(self, seconds: float, callback: Callable[[], None]) -> None:
        """Run ``callback`` every ``seconds``, from the next :meth:`start` on."""
        self._periodics.append(_Periodic(seconds, callback))

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    def start(self) -> None:
        if self._tasks:
            return
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._run(p)) for p in self._periodics]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    @staticmethod
    async def _run(periodic: _Periodic) -> None:
        while True:
            await asyncio.sleep(periodic.seconds)
            try:
                periodic.callback()
            except Exception:
                log.exception("scheduled callback failed")
