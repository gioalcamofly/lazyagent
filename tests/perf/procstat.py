"""CPU time of a process tree from /proc (Linux), for the idle-CPU budgets."""

from __future__ import annotations

import os
from pathlib import Path

_TICK = os.sysconf("SC_CLK_TCK")


def _stat(pid: int) -> tuple[int, float] | None:
    """(ppid, utime + stime in seconds)."""
    try:
        fields = (Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1]).split()
    except OSError:
        return None
    return int(fields[1]), (int(fields[11]) + int(fields[12])) / _TICK


def descendants(root: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            stat = _stat(int(entry.name))
            if stat:
                children.setdefault(stat[0], []).append(int(entry.name))
    out, todo = [], [root]
    while todo:
        pid = todo.pop()
        for child in children.get(pid, []):
            out.append(child)
            todo.append(child)
    return out


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def cpu_seconds(pids: list[int]) -> dict[int, float]:
    out = {}
    for pid in pids:
        stat = _stat(pid)
        if stat:
            out[pid] = stat[1]
    return out


def group(pid: int) -> str:
    line = cmdline(pid)
    if "--type=renderer" in line:
        return "chrome renderer"
    if "--type=gpu-process" in line:
        return "chrome gpu"
    if "chrome" in line:
        return "chrome other"
    if "lazyagent" in line:
        return "lazyagent (TUI + engine)"
    if "node" in line or "playwright" in line:
        return "playwright driver"
    return "other"
