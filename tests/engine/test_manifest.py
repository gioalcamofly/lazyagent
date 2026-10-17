"""The restart manifest on its own: what it keeps, what a new engine takes
over, and that writing it never runs on the event loop's thread."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time

import pytest

from lazyagent.engine import manifest as mf
from lazyagent.engine.manifest import Entry, Manifest


def _entry(path: str = "/r/feat", agent_id: str = "a1", **fields) -> Entry:
    return Entry(
        id=fields.pop("id", Entry.new_id()),
        worktree_path=path,
        agent_id=agent_id,
        kind=fields.pop("kind", "agent"),
        provider=fields.pop("provider", "claude"),
        **fields,
    )


def test_manifest_path_is_in_the_state_dir(tmp_path):
    path = mf.manifest_path("/r", {"XDG_STATE_HOME": str(tmp_path)})
    assert path.parent == tmp_path / "lazyagent" / "engines"
    assert path.name.endswith(".manifest.json")


def test_write_is_atomic_private_and_readable(tmp_path):
    path = tmp_path / "engines" / "x.manifest.json"
    entry = _entry(conversation_id="c-1", instruction="do it")
    mf.write(path, {"agents": [entry.to_dict()], "restorable": []})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp left
    agents, restorable = mf.read(path)
    assert agents == [entry] and restorable == []


def test_a_corrupt_manifest_is_moved_aside(tmp_path):
    path = tmp_path / "x.manifest.json"
    path.write_text("{not json")
    assert mf.read(path) == ([], [])
    assert not path.exists()
    assert (tmp_path / "x.manifest.json.corrupt").read_text() == "{not json"


def test_unknown_fields_and_bad_entries_are_ignored(tmp_path):
    path = tmp_path / "x.manifest.json"
    good = _entry().to_dict()
    path.write_text(
        json.dumps(
            {
                "agents": [{**good, "future_field": 1}, {"id": 3}, "nope"],
                "restorable": 7,
            }
        )
    )
    agents, restorable = mf.read(path)
    assert [e.id for e in agents] == [good["id"]] and restorable == []


def test_load_makes_earlier_agents_restorable_and_prunes_gone_worktrees(tmp_path):
    path = tmp_path / "m.json"
    running = _entry("/r/feat", "a2")
    old = _entry("/r/feat", "a1", ended_at=100.0)
    gone = _entry("/r/gone", "a1")
    mf.write(
        path,
        {
            "agents": [running.to_dict(), gone.to_dict()],
            "restorable": [old.to_dict()],
        },
    )
    manifest = Manifest(path, repo_root="/r", engine_id="new")
    manifest.load(lambda e: e.worktree_path != "/r/gone")
    assert set(manifest.restorable) == {running.id, old.id}
    assert manifest.restorable[running.id].ended_at is not None
    assert manifest.restorable[old.id].ended_at == 100.0
    assert manifest.agents == {}


def test_restorable_is_capped_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(mf, "RESTORABLE_LIMIT", 3)
    path = tmp_path / "m.json"
    entries = [_entry(agent_id=f"a{i}", ended_at=float(i)) for i in range(6)]
    mf.write(path, {"agents": [], "restorable": [e.to_dict() for e in entries]})
    manifest = Manifest(path, repo_root="/r")
    manifest.load(lambda e: True)
    assert sorted(e.agent_id for e in manifest.restorable.values()) == [
        "a3",
        "a4",
        "a5",
    ]


def test_instructions_are_clipped():
    assert mf.clip_instruction(None) is None
    assert len(mf.clip_instruction("x" * 10_000)) == mf.INSTRUCTION_LIMIT


@pytest.mark.asyncio
async def test_changes_are_written_debounced_and_off_the_loop_thread(tmp_path):
    loop_thread = threading.get_ident()
    calls: list[tuple[int, dict]] = []

    def writer(path, payload):
        calls.append((threading.get_ident(), payload))
        mf.write(path, payload)

    path = tmp_path / "m.json"
    manifest = Manifest(
        path, repo_root="/r", engine_id="e1", debounce=0.05, writer=writer
    )
    # A burst of changes on the loop: each costs the loop next to nothing.
    started = time.perf_counter()
    for i in range(200):
        manifest.add(_entry(agent_id=f"a{i}"))
    manifest.update("/r/feat", "a0", conversation_id="c-0")
    manifest.remove("/r/feat", "a199")
    on_loop = time.perf_counter() - started
    assert calls == []  # nothing written yet: debounced
    await asyncio.sleep(0.3)
    assert len(calls) == 1  # one write for the burst
    assert calls[0][0] != loop_thread  # in a worker thread
    agents, _ = mf.read(path)
    assert len(agents) == 199
    assert {e.engine_id for e in agents} == {"e1"}
    assert next(e for e in agents if e.agent_id == "a0").conversation_id == "c-0"
    assert on_loop < 0.05, f"200 changes took {on_loop * 1000:.1f} ms on the loop"


@pytest.mark.asyncio
async def test_close_flushes_a_pending_change(tmp_path):
    path = tmp_path / "m.json"
    manifest = Manifest(path, repo_root="/r", debounce=10)
    manifest.add(_entry())
    await manifest.close()
    assert len(mf.read(path)[0]) == 1
    assert manifest.writes == 1
    await manifest.close()  # nothing changed: no second write
    assert manifest.writes == 1


@pytest.mark.asyncio
async def test_take_and_prune(tmp_path):
    manifest = Manifest(tmp_path / "m.json", repo_root="/r", debounce=0)
    a, b = _entry(agent_id="a1"), _entry("/r/gone", "a2")
    manifest.restorable = {a.id: a, b.id: b}
    assert manifest.prune(lambda e: e.worktree_path != "/r/gone") == [b]
    assert manifest.take([a.id, "unknown"]) == [a]
    assert manifest.restorable == {}
    await manifest.close()
    assert mf.read(tmp_path / "m.json") == ([], [])


@pytest.mark.asyncio
async def test_a_failed_write_is_logged_not_raised(tmp_path, caplog):
    def broken(path, payload):
        raise OSError("disk full")

    manifest = Manifest(tmp_path / "m.json", repo_root="/r", writer=broken)
    manifest.add(_entry())
    await manifest.close()
    assert "disk full" in caplog.text
