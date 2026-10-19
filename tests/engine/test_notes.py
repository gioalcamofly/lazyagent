"""The session notes store (engine/notes.py): text operations, files on
disk, and the GC rules of docs/design/session-notes-plan.md §2."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from lazyagent.engine import notes as nt
from lazyagent.engine.notes import (
    LIVE,
    ORPHAN,
    RESTORABLE,
    NotesConflict,
    NotesError,
    NotesStore,
    NotesTooLarge,
)

DAY = 86400.0


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def events():
    return []


@pytest.fixture
def store(tmp_path, clock, events):
    return NotesStore(
        tmp_path / "notes",
        clock=clock,
        listener=lambda kind, data: events.append((kind, data)),
    )


def _reopen(store: NotesStore) -> NotesStore:
    again = NotesStore(store.root)
    again.load()
    return again


# --- Template and emptiness ----------------------------------------------


def test_create_writes_the_template(store):
    meta = store.create("/r/feat", "a1")
    notes = store.get(meta.notes_id)
    assert notes.text == (
        "## Goal\n\n## Constraints (don't touch)\n\n## Decisions\n\n## Doubts\n\n"
        "## Pending\n"
    )
    assert (meta.rev, meta.by, meta.state) == (0, None, LIVE)
    assert notes.empty
    assert store.path(meta.notes_id).read_text() == notes.text
    on_disk = json.loads((store.root / f"{meta.notes_id}.json").read_text())
    assert on_disk["worktree_path"] == "/r/feat" and on_disk["agent_id"] == "a1"
    assert on_disk["version"] == 1 and on_disk["mode"] == "propose"


def test_configured_sections_make_the_template(tmp_path):
    store = NotesStore(tmp_path, sections=["Scope", "Risks"])
    meta = store.create("/r", "a1")
    assert store.get(meta.notes_id).text == "## Scope\n\n## Risks\n"


def test_is_empty():
    assert nt.is_empty("")
    assert nt.is_empty(nt.template())
    assert nt.is_empty("## Goal\n\n\n## Whatever\n")
    assert not nt.is_empty("## Goal\nship it\n")
    assert not nt.is_empty("free text")


# --- put / append / replace_section --------------------------------------


def test_put_bumps_rev_and_announces(store, events, clock):
    meta = store.create("/r", "a1")
    clock.now += 5
    notes = store.put(meta.notes_id, "## Goal\nship\n", if_rev=0, by="user")
    assert (notes.meta.rev, notes.meta.by, notes.meta.updated_at) == (1, "user", clock.now)
    assert events == [
        (
            "updated",
            {
                "worktree_path": "/r",
                "agent_id": "a1",
                "notes_id": meta.notes_id,
                "rev": 1,
                "size": len("## Goal\nship\n"),
                "by": "user",
            },
        )
    ]
    assert _reopen(store).get(meta.notes_id).text == "## Goal\nship\n"


def test_put_with_a_stale_rev_is_a_conflict_carrying_the_current_text(store):
    meta = store.create("/r", "a1")
    store.put(meta.notes_id, "theirs\n", if_rev=0, by="agent")
    with pytest.raises(NotesConflict) as caught:
        store.put(meta.notes_id, "mine\n", if_rev=0, by="user")
    assert (caught.value.rev, caught.value.text) == (1, "theirs\n")
    assert "rev 1" in str(caught.value)
    assert store.get(meta.notes_id).text == "theirs\n"


def test_append_into_an_existing_section(store):
    meta = store.create("/r", "a1")
    store.append(meta.notes_id, "decisions", "use sqlite", by="agent")
    notes = store.append(meta.notes_id, "Decisions", "- keep WAL\nno ORM", by="agent")
    assert notes.text == (
        "## Goal\n\n## Constraints (don't touch)\n\n## Decisions\n- use sqlite\n"
        "- keep WAL\n- no ORM\n\n## Doubts\n\n## Pending\n"
    )
    assert notes.meta.rev == 2


def test_append_into_the_last_section_and_a_missing_one(store):
    meta = store.create("/r", "a1")
    store.append(meta.notes_id, "Pending", "- [ ] tests", by="user")
    notes = store.append(meta.notes_id, "Links", "PR 12", by="user")
    assert notes.text.endswith("## Pending\n- [ ] tests\n\n## Links\n- PR 12\n")


def test_append_keeps_indented_lines_and_checks_if_rev(store):
    meta = store.create("/r", "a1")
    notes = store.append(meta.notes_id, "Doubts", "- [ ] which db?\n  - answer: sqlite", by="agent")
    assert "## Doubts\n- [ ] which db?\n  - answer: sqlite\n\n## Pending" in notes.text
    with pytest.raises(NotesConflict):
        store.append(meta.notes_id, "Doubts", "x", by="agent", if_rev=0)
    with pytest.raises(NotesError, match="Nothing to append"):
        store.append(meta.notes_id, "Doubts", "\n \n", by="agent")
    with pytest.raises(NotesError, match="one-line"):
        store.append(meta.notes_id, "a\nb", "x", by="agent")


def test_replace_section_up_to_the_next_heading(store):
    meta = store.create("/r", "a1")
    text = "intro\n## Goal\nold\n### detail\nold too\n## Decisions\n- keep\n"
    store.put(meta.notes_id, text, if_rev=0, by="user")
    notes = store.replace_section(meta.notes_id, "goal", "new goal\n\n", if_rev=1, by="agent")
    assert notes.text == "intro\n## Goal\nnew goal\n\n## Decisions\n- keep\n"


def test_replace_section_at_the_end_of_the_file_and_missing(store):
    meta = store.create("/r", "a1")
    store.replace_section(meta.notes_id, "Pending", "- [ ] a\n- [ ] b", if_rev=0, by="agent")
    notes = store.replace_section(meta.notes_id, "Notes", "free", if_rev=1, by="agent")
    assert notes.text.endswith("## Pending\n- [ ] a\n- [ ] b\n\n## Notes\nfree\n")
    emptied = store.replace_section(meta.notes_id, "Goal", "", if_rev=2, by="agent")
    assert emptied.text.startswith("## Goal\n\n## Constraints")
    with pytest.raises(NotesConflict):
        store.replace_section(meta.notes_id, "Goal", "x", if_rev=0, by="agent")


def test_size_cap_and_warning(tmp_path):
    store = NotesStore(tmp_path, max_bytes=1024)
    meta = store.create("/r", "a1")
    small = store.put(meta.notes_id, "x" * 800, if_rev=0, by="user")
    assert small.size == 800 and small.warning is None
    near = store.put(meta.notes_id, "é" * 410, if_rev=1, by="user")  # 820 bytes
    assert near.size == 820 and "820 of 1024" in near.warning
    with pytest.raises(NotesTooLarge, match="1025 bytes \\(max 1024\\): condense them"):
        store.put(meta.notes_id, "x" * 1025, if_rev=2, by="user")
    with pytest.raises(NotesTooLarge):
        store.append(meta.notes_id, "Goal", "y" * 300, by="agent")
    assert store.get(meta.notes_id).meta.rev == 2  # refused writes change nothing


# --- Files ---------------------------------------------------------------


def test_files_are_private_and_written_atomically(store):
    meta = store.create("/r", "a1")
    store.put(meta.notes_id, "secret\n", if_rev=0, by="user")
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    for suffix in (".md", ".json"):
        path = store.root / f"{meta.notes_id}{suffix}"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in store.root.iterdir()) == sorted(
        [f"{meta.notes_id}.md", f"{meta.notes_id}.json"]
    )  # no temp file left


def test_delete_removes_every_file_and_announces(store, events):
    meta = store.create("/r", "a1")
    (store.root / f"{meta.notes_id}.seen.json").write_text("{}")  # the hook's
    gone = store.delete(meta.notes_id)
    assert gone.meta.notes_id == meta.notes_id
    assert list(store.root.iterdir()) == []
    assert events == [
        (
            "removed",
            {"notes_id": meta.notes_id, "worktree_path": "/r", "agent_id": "a1", "reason": "deleted"},
        )
    ]
    assert store.delete(meta.notes_id) is None


def test_load_round_trip_and_corrupt_meta_is_skipped(store, caplog):
    meta = store.create("/r", "a1")
    store.set_conversation(meta.notes_id, "conv-1")
    store.set_instruction(meta.notes_id, "fix the bug")
    store.set_instruction(meta.notes_id, "later prompt")  # first one wins
    (store.root / "0123456789ab.json").write_text("{not json")
    (store.root / "ba9876543210.json").write_text(json.dumps({"notes_id": 3}))
    (store.root / "aaaaaaaaaaaa.json").write_text(
        json.dumps({"notes_id": "aaaaaaaaaaaa", "worktree_path": "/r", "agent_id": "a9"})
    )  # no .md
    with caplog.at_level(logging.WARNING, logger="lazyagent.engine.notes"):
        again = _reopen(store)
    assert [m.notes_id for m in again.list()] == [meta.notes_id]
    loaded = again.get(meta.notes_id).meta
    assert (loaded.conversation_id, loaded.instruction) == ("conv-1", "fix the bug")
    skipped = " ".join(r.getMessage() for r in caplog.records)
    assert "0123456789ab" in skipped and "ba9876543210" in skipped
    assert "aaaaaaaaaaaa" in skipped


def test_a_missing_dir_loads_nothing(tmp_path):
    assert NotesStore(tmp_path / "nope").load() == 0


@pytest.mark.asyncio
async def test_writes_run_off_the_loop_thread_and_coalesce(tmp_path):
    loop_thread = threading.get_ident()
    calls = []
    release = threading.Event()

    def writer(root, notes_id, text, meta):
        calls.append((threading.get_ident(), text, meta["rev"]))
        release.wait(5)
        nt.write_notes(root, notes_id, text, meta)

    removed = []

    def remover(root, notes_id):
        removed.append(threading.get_ident())
        nt.remove_notes(root, notes_id)

    store = NotesStore(tmp_path, writer=writer, remover=remover)
    meta = store.create("/r", "a1")
    await asyncio.sleep(0.05)  # the template write is under way, blocked
    for rev in range(3):
        store.put(meta.notes_id, f"v{rev}\n", if_rev=rev, by="user")
    release.set()
    await store.flush()
    # The first write, then one for the three puts (the latest).
    assert [(text, rev) for _, text, rev in calls] == [(nt.template(), 0), ("v2\n", 3)]
    assert all(thread != loop_thread for thread, _, _ in calls)
    store.delete(meta.notes_id)
    await store.flush()
    assert removed and removed[0] != loop_thread
    assert list(tmp_path.iterdir()) == []


# --- GC rules (plan §2) --------------------------------------------------


def _filled(store: NotesStore, worktree: str, agent_id: str, text: str = "- note") -> str:
    meta = store.create(worktree, agent_id)
    store.append(meta.notes_id, "Goal", text, by="agent")
    return meta.notes_id


def _everywhere(meta) -> bool:
    return True


def test_gc_rule1_live_notes_are_always_kept(store):
    live = store.create("/r/feat", "a1").notes_id  # empty, too
    old = _filled(store, "/r/feat", "a2")
    store.mark(old, ORPHAN)
    assert store.gc({live}, {}, lambda meta: False) == [old]  # even gone: live
    assert live in store and store.get(live).meta.state == LIVE


def test_gc_rule2_only_the_newest_ended_notes_per_worktree(store, clock):
    a = _filled(store, "/r/feat", "a1")
    b = _filled(store, "/r/feat", "a2")
    c = _filled(store, "/r/feat", "a3")
    other = _filled(store, "/r/other", "a1")
    store.mark(c, ORPHAN)  # ended last, but an orphan now
    clock.now += 10
    restorable = {a: clock.now - 50, b: clock.now - 20, other: clock.now - 90}
    removed = store.gc(set(), restorable, _everywhere)
    assert sorted(removed) == sorted([a, b])
    assert c in store and other in store
    assert store.get(other).meta.state == RESTORABLE
    assert store.get(other).meta.ended_at == clock.now - 90


def test_gc_rule2_falls_back_to_updated_at(store, clock):
    a = _filled(store, "/r/feat", "a1")
    clock.now += 5
    b = _filled(store, "/r/feat", "a2")
    assert store.gc(set(), {a: None, b: None}, _everywhere) == [a]


def test_gc_rule3_orphans_expire(tmp_path, clock):
    store = NotesStore(tmp_path, orphan_days=7, clock=clock)
    old = _filled(store, "/r/a", "a1")
    store.mark(old, ORPHAN)
    clock.now += 3 * DAY
    young = _filled(store, "/r/b", "a1")
    store.mark(young, ORPHAN)
    assert store.expires_at(store.get(old).meta) == clock.now + 4 * DAY
    clock.now += 4 * DAY + 1
    assert store.gc(set(), {}, _everywhere) == [old]
    assert young in store


def test_gc_rule4_notes_of_a_gone_worktree(store):
    gone = _filled(store, "/r/gone", "a1")
    kept = _filled(store, "/r/here", "a1")
    store.mark(gone, ORPHAN)
    store.mark(kept, ORPHAN)
    assert store.gc(set(), {}, lambda meta: meta.worktree_path != "/r/gone") == [gone]


def test_gc_rule5_a_fresh_spawn_drops_the_worktrees_ended_notes(store):
    live = store.create("/r/feat", "a1").notes_id
    ended = _filled(store, "/r/feat", "a2")
    restorable = _filled(store, "/r/feat", "a3")
    elsewhere = _filled(store, "/r/other", "a2")
    store.mark(ended, ORPHAN)
    store.mark(restorable, RESTORABLE)
    assert sorted(store.delete_ended("/r/feat", {live})) == sorted([ended, restorable])
    assert live in store and elsewhere in store


def test_gc_drops_unreferenced_and_empty_ended_notes(store, events):
    stale = _filled(store, "/r/feat", "a1")  # live, but no agent runs it
    empty = store.create("/r/other", "a1").notes_id
    store.mark(empty, ORPHAN)
    events.clear()
    assert sorted(store.gc(set(), {}, _everywhere)) == sorted([stale, empty])
    assert {(k, d["reason"]) for k, d in events} == {("removed", "pruned")}


def test_gc_keeps_a_restorable_pinned_instruction(store):
    """Q-N23: template text, but the instruction its restore resumes."""
    pinned = store.create("/r/feat", "a1").notes_id
    store.set_instruction(pinned, "Build the importer")
    orphan = store.create("/r/other", "a1").notes_id
    store.set_instruction(orphan, "Nothing to adopt")
    store.mark(orphan, ORPHAN)
    assert store.gc(set(), {pinned: 100.0}, _everywhere) == [orphan]
    assert store.get(pinned).meta.state == RESTORABLE


def test_gc_without_a_manifest_only_expires(tmp_path, clock):
    store = NotesStore(tmp_path, clock=clock)
    mine = _filled(store, "/r/feat", "a1")  # live, not told: kept
    a = _filled(store, "/r/feat", "a2")
    b = _filled(store, "/r/feat", "a3")
    store.mark(a, ORPHAN)
    store.mark(b, ORPHAN)
    assert store.gc(set(), None, _everywhere) == []  # no "only the latest"
    clock.now += 8 * DAY
    assert sorted(store.gc(set(), None, _everywhere)) == sorted([a, b])
    assert mine in store


def test_mark_orphan_announces_once(store, events):
    meta = store.create("/r", "a1")
    assert store.mark(meta.notes_id, ORPHAN)
    assert not store.mark(meta.notes_id, ORPHAN)
    assert [d["reason"] for _, d in events] == ["orphaned"]
    store.revive(meta.notes_id, "/r", "a4")
    revived = store.get(meta.notes_id).meta
    assert (revived.state, revived.ended_at, revived.agent_id) == (LIVE, None, "a4")


def test_prune_engine_dirs(tmp_path):
    old, recent = tmp_path / "engine-old", tmp_path / "engine-new"
    for directory in (old, recent):
        directory.mkdir()
        (directory / "x.md").write_text("x")
    past = time.time() - 8 * DAY
    os.utime(old / "x.md", (past, past))
    (tmp_path / "abcabcabcabc.md").write_text("top level: not touched")
    assert nt.prune_engine_dirs(tmp_path, 7) == [old]
    assert not old.exists() and recent.exists()
    assert (tmp_path / "abcabcabcabc.md").exists()


def test_notes_dir_is_in_the_state_dir(tmp_path):
    path = nt.notes_dir("/home/u/repo", {"XDG_STATE_HOME": str(tmp_path)})
    assert path.parent == tmp_path / "lazyagent" / "notes"
    assert not str(path).startswith("/home/u/repo")
    assert isinstance(path, Path)
