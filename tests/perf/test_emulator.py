"""Throughput of the engine's headless emulator (pyte + ScrollbackScreen),
on the two workloads of §7d-4 at 120×40 in 4 KiB chunks. Fails below 0.8×
the baseline stored in baseline.json (measured on the author's machine;
re-measure with ``LAZYAGENT_PERF_REBASE=1`` after an intended change)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pyte
import pytest
from lazyagent.engine.screen import ScrollbackScreen

pytestmark = pytest.mark.perf

BASELINE = Path(__file__).with_name("baseline.json")
SIZE = 2_000_000
CHUNK = 4096


def plain() -> str:
    line = "x" * 97 + "\r\n"
    return (line * (SIZE // len(line) + 1))[:SIZE]


def agent_like() -> str:
    """Ink-style output: spinner redraws, a box re-rendered with cursor-up
    and erase-line, SGR 256 colours, coloured diff lines."""
    parts = []
    spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while sum(map(len, parts)) < SIZE:
        for s in spinner:
            parts.append(f"\r\x1b[2K\x1b[38;5;{208 + i % 6}m{s}\x1b[0m Thinking… ({i}s)")
        box = ["╭" + "─" * 70 + "╮"] + [
            f"│ \x1b[38;5;{i % 200}mline {n} of the plan\x1b[0m".ljust(80) + "│" for n in range(6)
        ] + ["╰" + "─" * 70 + "╯"]
        parts.append("\r\n".join(box) + f"\x1b[{len(box) - 1}A\r")
        parts.append("\x1b[8B\r\n")
        for n in range(10):
            sign, color = ("+", 32) if n % 2 else ("-", 31)
            parts.append(f"\x1b[{color}m{sign} def f{n}(x): return x * {i}\x1b[0m\r\n")
        i += 1
    return "".join(parts)[:SIZE]


def throughput(text: str) -> float:
    screen = ScrollbackScreen(120, 40)
    stream = pyte.Stream(screen)
    start = time.perf_counter()
    for i in range(0, len(text), CHUNK):
        stream.feed(text[i : i + CHUNK])
    elapsed = time.perf_counter() - start
    return len(text.encode()) / elapsed / 1e6


@pytest.mark.parametrize("workload", ["plain", "agent"])
def test_emulator_throughput(workload, record):
    text = plain() if workload == "plain" else agent_like()
    mb_s = max(throughput(text) for _ in range(3))
    record(f"emulator {workload} (MB/s)", mb_s=round(mb_s, 2))
    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    if os.environ.get("LAZYAGENT_PERF_REBASE") or workload not in baseline:
        baseline[workload] = round(mb_s, 2)
        BASELINE.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
        return
    assert mb_s >= 0.8 * baseline[workload], (mb_s, baseline[workload])
