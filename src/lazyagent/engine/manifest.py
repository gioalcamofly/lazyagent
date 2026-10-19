"""The restart manifest: which agents an engine runs, kept on disk so the
next engine for the repo can offer to bring them back (PR 9, K3).

One JSON file per repo in the state dir
(``$XDG_STATE_HOME/lazyagent/engines/<hash>.manifest.json``, next to the
engine log; the runtime dir is wiped at logout and a reboot is the case this
is for). It holds:

- ``agents``: what this engine runs now, one :class:`Entry` each, updated on
  every spawn, exit and stop, and when an agent's identity changes (its
  provider conversation id, its last instruction);
- ``restorable``: what the engines before it ran when they ended (stopped,
  crashed, or the machine went down) and nobody has restored or dismissed
  yet.

A new engine reads the file at start: the old ``agents`` and ``restorable``
together are its restorable set. Entries whose worktree is gone are pruned.

Writes never block the event loop: a change marks the manifest dirty, and a
debounced task writes it from a worker thread (temp file, ``fsync``,
``os.replace``), at most one write in flight; :meth:`Manifest.close` flushes
what is left. Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from lazyagent.engine.runtime import repo_digest, state_dir

log = logging.getLogger(__name__)

VERSION = 1
DEBOUNCE = 0.2
# Restorable entries kept, newest first; older ones are dropped.
RESTORABLE_LIMIT = 50
# Longest instruction kept per agent (the head of it).
INSTRUCTION_LIMIT = 4096


def manifest_path(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    return state_dir(environ) / "engines" / f"{repo_digest(repo_root)}.manifest.json"


@dataclass
class Entry:
    """One agent, as the manifest remembers it."""

    id: str
    worktree_path: str
    agent_id: str
    kind: str
    provider: str
    label: str = ""
    session_name: str = ""
    skip_permissions: bool = False
    # The provider's own conversation id (Claude's session_id), when its
    # observer reported one: a restore resumes exactly that conversation.
    conversation_id: str | None = None
    instruction: str | None = None
    started_at: float = field(default_factory=time.time)
    engine_id: str | None = None
    # Set on restorable entries: when the engine that ran it ended.
    ended_at: float | None = None

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Entry | None:
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        try:
            entry = cls(**values)
        except TypeError:
            return None
        if not all(isinstance(v, str) for v in (entry.id, entry.worktree_path)):
            return None
        return entry


def _ended(entry: Entry) -> float:
    return entry.started_at if entry.ended_at is None else entry.ended_at


def clip_instruction(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:INSTRUCTION_LIMIT]


def read(path: Path) -> tuple[list[Entry], list[Entry]]:
    """``(agents, restorable)`` from a manifest file; empty when it is
    missing or unreadable (a corrupt file is moved aside)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError):
        log.warning("unreadable restart manifest %s; starting without it", path)
        try:
            os.replace(path, path.with_name(path.name + ".corrupt"))
        except OSError:
            pass
        return [], []
    if not isinstance(data, dict):
        return [], []

    def entries(key: str) -> list[Entry]:
        items = data.get(key)
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if isinstance(item, dict) and (entry := Entry.from_dict(item)):
                out.append(entry)
        return out

    return entries("agents"), entries("restorable")


def write(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace ``path`` with ``payload`` (durable: fsynced)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, indent=1).encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Manifest:
    """The manifest of one engine: its live entries, its restorable set, and
    the debounced writer."""

    def __init__(
        self,
        path: Path,
        *,
        repo_root: str,
        engine_id: str | None = None,
        debounce: float = DEBOUNCE,
        writer: Callable[[Path, dict[str, Any]], None] = write,
    ) -> None:
        self.path = path
        self.repo_root = repo_root
        self.engine_id = engine_id
        self.agents: dict[tuple[str, str], Entry] = {}
        self.restorable: dict[str, Entry] = {}
        self._debounce = debounce
        self._writer = writer
        self._dirty = False
        self._task: asyncio.Task | None = None
        # True while the debounce task only sleeps: the one moment it may be
        # cancelled. A write in progress must finish (its thread would go on
        # anyway), so writes are serialized by a lock, created on first use
        # (on the loop that writes).
        self._sleeping = False
        self._lock: asyncio.Lock | None = None
        self.writes = 0

    # ------------------------------------------------------------------
    # Loading and pruning
    # ------------------------------------------------------------------

    def load(self, exists: Callable[[Entry], bool]) -> None:
        """Take over what the engines before this one left: every entry
        becomes restorable, unless ``exists`` says its worktree is gone."""
        agents, restorable = read(self.path)
        now = time.time()
        for entry in agents:
            if entry.ended_at is None:
                entry.ended_at = now
        merged = {e.id: e for e in (*restorable, *agents)}
        # One entry per agent (worktree, id; the orchestrator's id is ""):
        # every stop leaves one, and restoring an agent twice cannot work.
        newest: dict[tuple[str, str], Entry] = {}
        for entry in sorted(merged.values(), key=_ended):
            newest[(entry.worktree_path, entry.agent_id)] = entry
        self.restorable = {e.id: e for e in newest.values() if exists(e)}
        self._cap()
        if merged:
            self.mark_dirty()

    def prune(self, exists: Callable[[Entry], bool]) -> list[Entry]:
        """Drop restorable entries whose worktree is gone; the dropped."""
        gone = [e for e in self.restorable.values() if not exists(e)]
        for entry in gone:
            del self.restorable[entry.id]
        if gone:
            self.mark_dirty()
        return gone

    def _cap(self) -> None:
        if len(self.restorable) > RESTORABLE_LIMIT:
            newest = sorted(self.restorable.values(), key=_ended, reverse=True)[
                :RESTORABLE_LIMIT
            ]
            self.restorable = {e.id: e for e in newest}

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    def add(self, entry: Entry) -> None:
        entry.engine_id = self.engine_id
        self.agents[(entry.worktree_path, entry.agent_id)] = entry
        self.mark_dirty()

    def remove(self, worktree_path: str, agent_id: str) -> None:
        if self.agents.pop((worktree_path, agent_id), None) is not None:
            self.mark_dirty()

    def get(self, worktree_path: str, agent_id: str) -> Entry | None:
        return self.agents.get((worktree_path, agent_id))

    def update(self, worktree_path: str, agent_id: str, **changes: Any) -> bool:
        """Change a live entry; whether anything changed."""
        entry = self.agents.get((worktree_path, agent_id))
        if entry is None:
            return False
        changed = False
        for name, value in changes.items():
            if getattr(entry, name) != value:
                setattr(entry, name, value)
                changed = True
        if changed:
            self.mark_dirty()
        return changed

    def take(self, ids: Iterable[str]) -> list[Entry]:
        """Remove restorable entries (to restore or dismiss them)."""
        taken = [self.restorable.pop(i) for i in ids if i in self.restorable]
        if taken:
            self.mark_dirty()
        return taken

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "repo_root": self.repo_root,
            "engine_id": self.engine_id,
            "updated_at": time.time(),
            "agents": [e.to_dict() for e in self.agents.values()],
            "restorable": [e.to_dict() for e in self.restorable.values()],
        }

    def mark_dirty(self) -> None:
        """Schedule a write (debounced; never on the loop's thread)."""
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (yet): close() or the next change writes it
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._write_soon())

    async def _write_soon(self) -> None:
        while self._dirty:
            self._sleeping = True
            try:
                await asyncio.sleep(self._debounce)
            finally:
                self._sleeping = False
            await self._write()

    async def _write(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            # The snapshot is taken on the loop (a few dicts); encoding,
            # writing and fsync happen in a worker thread.
            payload = self.payload()
            try:
                await asyncio.to_thread(self._writer, self.path, payload)
                self.writes += 1
            except OSError as e:
                log.warning(
                    "could not write the restart manifest %s: %s", self.path, e
                )

    async def flush(self) -> None:
        """Write now if anything changed, and wait for it, and for a write
        already under way."""
        task = self._task
        if task is not None and not task.done() and self._sleeping:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._write()

    async def close(self) -> None:
        """Flush; the running agents stay in the file, so the next engine
        offers them back."""
        await self.flush()
