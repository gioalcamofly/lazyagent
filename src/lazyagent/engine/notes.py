"""Session notes: one Markdown file per agent slot, owned by the engine
(proposal 0011, ``docs/design/session-notes-plan.md`` §2).

The files live in the state dir, never in a worktree:
``$XDG_STATE_HOME/lazyagent/notes/<repo hash>/`` (0700), and per notes id

- ``<id>.md``: the text (0600). Only the engine writes it.
- ``<id>.json``: the meta (:class:`Meta`), written after the ``.md``.
- ``<id>.seen.json``: what the agent last saw, written by the notes hook
  only (N3); the store just deletes it with the rest.

:class:`NotesStore` keeps every meta and text in memory; a change bumps
``rev`` on the caller's thread (the engine's loop, which serializes
writers) and schedules the write of that id from a worker thread (temp
file, ``fsync``, ``os.replace``, as the restart manifest), at most one write
in flight per id. Without a running loop (unit tests, scripts) it writes
at once. :meth:`NotesStore.gc` applies the "only the latest" rules (plan
§2). Standard library only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from lazyagent.engine.runtime import repo_digest, state_dir

log = logging.getLogger(__name__)

VERSION = 1
DEFAULT_SECTIONS = (
    "Goal",
    "Constraints (don't touch)",
    "Decisions",
    "Doubts",
    "Pending",
)
DEFAULT_MAX_BYTES = 8192
DEFAULT_ORPHAN_DAYS = 7
# Results carry a warning from this share of max_bytes on.
WARN_RATIO = 0.8
MODES = ("manual", "propose", "free")

LIVE = "live"
RESTORABLE = "restorable"
ORPHAN = "orphan"
STATES = (LIVE, RESTORABLE, ORPHAN)

# notes.removed reasons.
DELETED = "deleted"  # on purpose: stop, dismiss, a client
ORPHANED = "orphaned"  # the agent ended without a stop: kept, but no longer live
PRUNED = "pruned"  # by the GC rules
ADOPTED = "adopted"  # their text moved into a live agent's notes

_HEADING = re.compile(r"^##[ \t]+(.*?)\s*$")


def notes_dir(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    return state_dir(environ) / "notes" / repo_digest(repo_root)


def template(sections: Iterable[str] = DEFAULT_SECTIONS) -> str:
    return "\n\n".join(f"## {s}" for s in sections) + "\n"


def is_empty(text: str) -> bool:
    """Whether ``text`` holds nothing but ``##`` headings (the template, or
    what is left of it): nothing to re-inject, hand off or keep."""
    return all(_HEADING.match(line) for line in text.splitlines() if line.strip())


class NotesError(ValueError):
    pass


class NotesConflict(NotesError):
    """A write's ``if_rev`` is not the current rev; carries the current
    text so the writer can merge."""

    def __init__(self, rev: int, text: str) -> None:
        super().__init__(f"notes changed (rev {rev}): read them again")
        self.rev = rev
        self.text = text


class NotesTooLarge(NotesError):
    def __init__(self, size: int, max_bytes: int) -> None:
        super().__init__(
            f"notes would be {size} bytes (max {max_bytes}): condense them"
        )
        self.size = size
        self.max_bytes = max_bytes


@dataclass
class Meta:
    """What the store knows of one notes file besides its text."""

    notes_id: str
    worktree_path: str
    agent_id: str
    kind: str = "agent"
    conversation_id: str | None = None
    rev: int = 0
    # Who wrote last: user, agent or orchestrator; None for the template.
    by: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    # When it stopped being live (restorable or orphan).
    ended_at: float | None = None
    state: str = LIVE
    mode: str = "propose"
    # The pinned instruction (N3): the first prompt, first write wins.
    instruction: str | None = None
    version: int = VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Meta | None:
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            meta = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        ok = (
            all(
                isinstance(v, str)
                for v in (meta.notes_id, meta.worktree_path, meta.agent_id)
            )
            and isinstance(meta.rev, int)
            and isinstance(meta.updated_at, (int, float))
            and (meta.ended_at is None or isinstance(meta.ended_at, (int, float)))
            and meta.state in STATES
            and _ID.fullmatch(meta.notes_id) is not None
        )
        return meta if ok else None


_ID = re.compile(r"[0-9a-f]{12}")


@dataclass(frozen=True)
class Notes:
    """One notes file as a caller sees it (a copy)."""

    meta: Meta
    text: str
    max_bytes: int

    @property
    def size(self) -> int:
        return len(self.text.encode())

    @property
    def warning(self) -> str | None:
        if self.size >= self.max_bytes * WARN_RATIO:
            return (
                f"notes are {self.size} of {self.max_bytes} bytes: condense them "
                "before they fill up"
            )
        return None

    @property
    def empty(self) -> bool:
        return is_empty(self.text)


Listener = Callable[[str, dict[str, Any]], None]


# ----------------------------------------------------------------------
# Files (worker thread)
# ----------------------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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


def write_notes(root: Path, notes_id: str, text: str, meta: dict[str, Any]) -> None:
    """Write ``<id>.md`` then ``<id>.json`` atomically (dir 0700, files 0600)."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    _atomic_write(root / f"{notes_id}.md", text.encode())
    _atomic_write(root / f"{notes_id}.json", json.dumps(meta, indent=1).encode())


def remove_notes(root: Path, notes_id: str) -> None:
    for suffix in (".json", ".md", ".seen.json"):
        (root / f"{notes_id}{suffix}").unlink(missing_ok=True)


def prune_engine_dirs(
    root: Path, orphan_days: float, now: float | None = None
) -> list[Path]:
    """Remove the dirs of engines that kept no manifest (``engine-*``, see
    ``Engine._notes_store``) when nothing in them changed for
    ``orphan_days``: those engines are long gone. The removed dirs."""
    now = time.time() if now is None else now
    removed = []
    try:
        dirs = [d for d in root.glob("engine-*") if d.is_dir()]
    except OSError:
        return []
    for directory in dirs:
        try:
            files = list(directory.iterdir())
            newest = max(
                (f.stat().st_mtime for f in files), default=directory.stat().st_mtime
            )
            if now - newest < orphan_days * 86400:
                continue
            for f in files:
                f.unlink(missing_ok=True)
            directory.rmdir()
            removed.append(directory)
        except OSError:
            continue
    return removed


# ----------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """``(heading index, end)`` of the first ``## name`` section (case
    insensitive); it ends at the next ``##`` heading or the end."""
    wanted = name.strip().casefold()
    start = None
    for i, line in enumerate(lines):
        match = _HEADING.match(line)
        if match is None:
            continue
        if start is not None:
            return start, i
        if match.group(1).casefold() == wanted:
            start = i
    return None if start is None else (start, len(lines))


def _join(lines: list[str]) -> str:
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def _with_section(lines: list[str], heading: str) -> tuple[int, int]:
    """Add ``## heading`` at the end of ``lines``; its bounds."""
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append(f"## {heading}")
    return len(lines) - 1, len(lines)


def _bullets(text: str) -> list[str]:
    """Each non-blank line of ``text`` as a list item: lines already items
    (``- ``) or indented (continuations) are kept as they are."""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        out.append(line if line.startswith(("- ", " ", "\t")) else f"- {line.strip()}")
    return out


# ----------------------------------------------------------------------
# The store
# ----------------------------------------------------------------------


class NotesStore:
    """Every notes file of one repo (one engine's view of it)."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        sections: Iterable[str] = DEFAULT_SECTIONS,
        orphan_days: float = DEFAULT_ORPHAN_DAYS,
        clock: Callable[[], float] = time.time,
        listener: Listener | None = None,
        writer: Callable[[Path, str, str, dict[str, Any]], None] = write_notes,
        remover: Callable[[Path, str], None] = remove_notes,
    ) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.sections = tuple(sections)
        self.orphan_days = orphan_days
        self.listener = listener
        self._clock = clock
        self._writer = writer
        self._remover = remover
        self._metas: dict[str, Meta] = {}
        self._texts: dict[str, str] = {}
        # Ids changed since their last write, and the task writing each.
        self._dirty: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self.writes = 0

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> int:
        """Read what is on disk (at engine start, as the manifest); corrupt
        or half-written files are skipped and logged. How many loaded."""
        try:
            paths = sorted(self.root.glob("*.json"))
        except OSError:
            return 0
        for path in paths:
            if path.name.endswith(".seen.json"):
                continue
            try:
                meta = Meta.from_dict(json.loads(path.read_text()))
            except (OSError, ValueError):
                meta = None
            if meta is None or path.stem != meta.notes_id:
                log.warning("skipping unreadable notes meta %s", path)
                continue
            try:
                text = (self.root / f"{meta.notes_id}.md").read_text()
            except (OSError, ValueError):
                log.warning("skipping notes %s: no readable text", meta.notes_id)
                continue
            self._metas[meta.notes_id] = meta
            self._texts[meta.notes_id] = text
        return len(self._metas)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def path(self, notes_id: str) -> Path:
        """Where ``<id>.md`` is (or will be)."""
        return self.root / f"{notes_id}.md"

    def __contains__(self, notes_id: object) -> bool:
        return notes_id in self._metas

    def get(self, notes_id: str) -> Notes:
        meta = self._meta(notes_id)
        return Notes(replace(meta), self._texts[notes_id], self.max_bytes)

    def list(self, worktree: str | None = None) -> list[Meta]:
        """Copies of every meta (of ``worktree`` only, when given), newest
        first."""
        metas = [
            replace(m)
            for m in self._metas.values()
            if worktree is None or m.worktree_path == worktree
        ]
        return sorted(metas, key=_ended, reverse=True)

    def section_bullets(self, notes_id: str, name: str) -> list[str]:
        """The list items of section ``name`` (case insensitive), without
        their ``- ``; continuation lines are dropped. Empty when the notes
        or the section are missing."""
        text = self._texts.get(notes_id)
        if text is None:
            return []
        lines = text.splitlines()
        bounds = _section_bounds(lines, name)
        if bounds is None:
            return []
        start, end = bounds
        return [
            line[2:].strip()
            for line in lines[start + 1 : end]
            if line.startswith("- ") and line[2:].strip()
        ]

    def is_empty(self, text: str) -> bool:
        return is_empty(text)

    def expires_at(self, meta: Meta) -> float | None:
        if meta.state != ORPHAN:
            return None
        return _ended(meta) + self.orphan_days * 86400

    def _meta(self, notes_id: str) -> Meta:
        meta = self._metas.get(notes_id)
        if meta is None:
            raise NotesError(f"No notes {notes_id!r}")
        return meta

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(
        self,
        worktree: str,
        agent_id: str,
        kind: str = "agent",
        *,
        mode: str = "propose",
        notes_id: str | None = None,
    ) -> Meta:
        """New live notes for a slot, from the template."""
        notes_id = notes_id or uuid.uuid4().hex[:12]
        now = self._clock()
        meta = Meta(
            notes_id=notes_id,
            worktree_path=worktree,
            agent_id=agent_id,
            kind=kind,
            created_at=now,
            updated_at=now,
            mode=mode,
        )
        self._metas[notes_id] = meta
        self._texts[notes_id] = template(self.sections)
        self._schedule(notes_id)
        return replace(meta)

    def revive(self, notes_id: str, worktree: str, agent_id: str) -> Meta:
        """Make restorable or orphan notes the live notes of a slot (a
        restore; the agent id may be a new one)."""
        meta = self._meta(notes_id)
        meta.worktree_path, meta.agent_id = worktree, agent_id
        meta.state, meta.ended_at = LIVE, None
        self._schedule(notes_id)
        return replace(meta)

    def set_conversation(self, notes_id: str, conversation_id: str | None) -> bool:
        meta = self._metas.get(notes_id)
        if meta is None or conversation_id in (None, "", meta.conversation_id):
            return False
        meta.conversation_id = conversation_id
        self._schedule(notes_id)
        return True

    def set_instruction(self, notes_id: str, text: str | None) -> bool:
        """Pin the instruction; the first one wins."""
        meta = self._metas.get(notes_id)
        if meta is None or not text or meta.instruction is not None:
            return False
        meta.instruction = text
        self._schedule(notes_id)
        return True

    def mark(self, notes_id: str, state: str, ended_at: float | None = None) -> bool:
        """Change the state; leaving ``live`` stamps ``ended_at`` (now unless
        given). Becoming an orphan is announced (``orphaned``)."""
        if state not in STATES:
            raise ValueError(f"Unknown notes state: {state!r}")
        meta = self._metas.get(notes_id)
        if meta is None:
            return False
        if state == LIVE:
            ended_at = None
        elif ended_at is None:
            ended_at = meta.ended_at if meta.state != LIVE else self._clock()
        if (meta.state, meta.ended_at) == (state, ended_at):
            return False
        was_live = meta.state == LIVE
        meta.state, meta.ended_at = state, ended_at
        self._schedule(notes_id)
        if state == ORPHAN and was_live:
            self._removed(meta, ORPHANED)
        return True

    def delete(self, notes_id: str, reason: str = DELETED) -> Notes | None:
        """Drop the notes (memory now, disk soon); what they were."""
        meta = self._metas.pop(notes_id, None)
        if meta is None:
            return None
        text = self._texts.pop(notes_id)
        self._schedule(notes_id)
        self._removed(meta, reason)
        return Notes(meta, text, self.max_bytes)

    # ------------------------------------------------------------------
    # Writing the text
    # ------------------------------------------------------------------

    def put(self, notes_id: str, text: str, if_rev: int, by: str) -> Notes:
        """Replace the whole text (a client's editor)."""
        self._check_rev(notes_id, if_rev)
        return self._write(notes_id, text, by)

    def append(
        self, notes_id: str, section: str, text: str, by: str, if_rev: int | None = None
    ) -> Notes:
        """Add ``text`` as list items at the end of ``section`` (created at
        the end of the file when missing)."""
        section = self._section_name(section)
        items = _bullets(text)
        if not items:
            raise NotesError("Nothing to append")
        if if_rev is not None:
            self._check_rev(notes_id, if_rev)
        lines = self._meta_text(notes_id).splitlines()
        bounds = _section_bounds(lines, section) or _with_section(lines, section)
        start, end = bounds
        last = start
        for i in range(start, end):
            if lines[i].strip():
                last = i
        lines[last + 1 : last + 1] = items
        after = last + 1 + len(items)
        if after < len(lines) and _HEADING.match(lines[after]):
            lines.insert(after, "")
        return self._write(notes_id, _join(lines), by)

    def replace_section(
        self, notes_id: str, section: str, text: str, if_rev: int, by: str
    ) -> Notes:
        """Replace the body of ``section`` (up to the next ``##`` heading)."""
        section = self._section_name(section)
        self._check_rev(notes_id, if_rev)
        lines = self._meta_text(notes_id).splitlines()
        start, end = _section_bounds(lines, section) or _with_section(lines, section)
        body = text.strip("\n").splitlines() if text.strip() else []
        lines[start + 1 : end] = body + ([""] if end < len(lines) else [])
        return self._write(notes_id, _join(lines), by)

    def _section_name(self, section: str) -> str:
        name = section.strip()
        if not name or "\n" in name:
            raise NotesError("A section is a one-line heading name")
        for known in self.sections:
            if known.casefold() == name.casefold():
                return known
        return name

    def _meta_text(self, notes_id: str) -> str:
        self._meta(notes_id)
        return self._texts[notes_id]

    def _check_rev(self, notes_id: str, if_rev: int) -> None:
        meta = self._meta(notes_id)
        if if_rev != meta.rev:
            raise NotesConflict(meta.rev, self._texts[notes_id])

    def _write(self, notes_id: str, text: str, by: str) -> Notes:
        size = len(text.encode())
        if size > self.max_bytes:
            raise NotesTooLarge(size, self.max_bytes)
        meta = self._meta(notes_id)
        meta.rev += 1
        meta.by = by
        meta.updated_at = self._clock()
        self._texts[notes_id] = text
        self._schedule(notes_id)
        notes = Notes(replace(meta), text, self.max_bytes)
        self._emit(
            "updated",
            {
                "worktree_path": meta.worktree_path,
                "agent_id": meta.agent_id,
                "notes_id": notes_id,
                "rev": meta.rev,
                "size": notes.size,
                "by": by,
            },
        )
        return notes

    # ------------------------------------------------------------------
    # GC ("only the latest", plan §2)
    # ------------------------------------------------------------------

    def gc(
        self,
        live_ids: Collection[str],
        restorable: Mapping[str, float | None] | None,
        exists: Callable[[Meta], bool],
    ) -> list[str]:
        """Apply the GC rules; the ids removed.

        ``live_ids``: notes of the agents running now (always kept).
        ``restorable``: notes id → ``ended_at`` of the restorable entries,
        or None for an engine without a manifest (then only the orphan TTL
        and gone worktrees apply). ``exists``: whether a meta's worktree is
        still there.
        """
        removed: list[str] = []

        def drop(meta: Meta) -> None:
            if self.delete(meta.notes_id, PRUNED) is not None:
                removed.append(meta.notes_id)

        now = self._clock()
        kept: dict[str, list[Meta]] = {}
        for meta in list(self._metas.values()):
            notes_id = meta.notes_id
            if notes_id in live_ids:
                self.mark(notes_id, LIVE)
                continue
            if not exists(meta):
                drop(meta)
                continue
            if restorable is not None and notes_id in restorable:
                ended = restorable[notes_id]
                if ended is None:
                    ended = meta.ended_at or meta.updated_at
                self.mark(notes_id, RESTORABLE, ended)
            elif meta.state != ORPHAN and restorable is not None:
                drop(meta)  # no entry and no orphan right
                continue
            elif meta.state == LIVE:
                continue  # an engine without a manifest: its own agents' notes
            expires = self.expires_at(meta)
            if expires is not None and expires <= now:
                drop(meta)
                continue
            if is_empty(self._texts[notes_id]) and not (
                meta.state == RESTORABLE and meta.instruction
            ):
                # Nothing to keep; a restorable agent's pinned instruction
                # is (its restore brings the same conversation back, Q-N23).
                drop(meta)
                continue
            kept.setdefault(meta.worktree_path, []).append(meta)
        if restorable is not None:
            for metas in kept.values():
                # Ties (the agents of one engine end together): the newest agent.
                newest = sorted(
                    metas, key=lambda m: (_ended(m), m.created_at), reverse=True
                )
                for meta in newest[1:]:
                    drop(meta)
        return removed

    def delete_ended(self, worktree: str, live_ids: Collection[str]) -> list[str]:
        """Rule 5: a fresh agent in ``worktree`` is its most recent one;
        every ended agent's notes there go."""
        gone = [
            m.notes_id
            for m in list(self._metas.values())
            if m.worktree_path == worktree and m.notes_id not in live_ids
        ]
        for notes_id in gone:
            self.delete(notes_id, PRUNED)
        return gone

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _schedule(self, notes_id: str) -> None:
        self._dirty.add(notes_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop, so no loop thread to keep free: write now.
            self._dirty.discard(notes_id)
            self._persist(notes_id, self._snapshot(notes_id))
            return
        task = self._tasks.get(notes_id)
        if task is None or task.done():
            self._tasks[notes_id] = loop.create_task(self._write_soon(notes_id))

    def _snapshot(self, notes_id: str) -> tuple[str, dict[str, Any]] | None:
        meta = self._metas.get(notes_id)
        if meta is None:
            return None
        return self._texts[notes_id], meta.to_dict()

    async def _write_soon(self, notes_id: str) -> None:
        while notes_id in self._dirty:
            self._dirty.discard(notes_id)
            snapshot = self._snapshot(notes_id)
            await asyncio.to_thread(self._persist, notes_id, snapshot)
        self._tasks.pop(notes_id, None)

    def _persist(
        self, notes_id: str, snapshot: tuple[str, dict[str, Any]] | None
    ) -> None:
        try:
            if snapshot is None:
                self._remover(self.root, notes_id)
            else:
                self._writer(self.root, notes_id, *snapshot)
                self.writes += 1
        except OSError as e:
            log.warning("could not write notes %s in %s: %s", notes_id, self.root, e)

    async def flush(self) -> None:
        """Wait for every pending write."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _removed(self, meta: Meta, reason: str) -> None:
        self._emit(
            "removed",
            {
                "notes_id": meta.notes_id,
                "worktree_path": meta.worktree_path,
                "agent_id": meta.agent_id,
                "reason": reason,
            },
        )

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.listener is not None:
            try:
                self.listener(kind, data)
            except Exception:
                log.exception("notes listener failed on %s", kind)


def _ended(meta: Meta) -> float:
    return meta.updated_at if meta.ended_at is None else meta.ended_at
