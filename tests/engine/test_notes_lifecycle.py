"""Session notes through the engine: the lifecycle table of proposal 0011
(real PTYs, the fake agent, the restart world of test_restore.py)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import threading

import pytest
import pytest_asyncio

from lazyagent.agent_providers import NOTES_PATH_ENV, ResumeMode
from lazyagent.engine import manifest as mf
from lazyagent.engine import notes as nt
from lazyagent.engine.engine import ORCHESTRATOR_KEY, Engine
from lazyagent.engine.notes import LIVE, ORPHAN, RESTORABLE
from tests.engine.fakes import fake_agent_provider, hooks_dir, make_engine, worktree
from tests.engine.test_restore import World, _conversation, _events, _until

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]


@pytest_asyncio.fixture
async def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        await w.stop_all()


def _write(engine: Engine, key: str, agent_id: str, text: str, by: str = "agent") -> str:
    notes_id = engine.notes_id(key, agent_id)
    engine.notes.append(notes_id, "Decisions", text, by=by)
    return notes_id


def _removed(events, notes_id: str) -> list[str]:
    return [
        e.data["reason"]
        for e in events
        if e.event == "notes.removed" and e.data["notes_id"] == notes_id
    ]


async def test_a_spawn_creates_notes_outside_the_worktree(world):
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = engine.notes_id(feat, agent.agent_id)
    assert engine.manifest.get(feat, agent.agent_id).notes_id == notes_id
    notes = engine.notes.get(notes_id)
    assert notes.empty and notes.meta.state == LIVE
    assert (notes.meta.worktree_path, notes.meta.agent_id) == (feat, agent.agent_id)
    assert notes.meta.mode == "propose"
    await engine.notes.flush()
    path = engine.notes.path(notes_id)
    assert path.read_text() == notes.text
    assert engine.notes.root == nt.notes_dir(str(world.repo))
    for root in (world.repo, world.feat):
        assert not path.is_relative_to(root)


async def test_events_carry_the_writer(world):
    engine = world.engine()
    events = _events(engine)  # strict schema validation of every event
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "use sqlite", by="orchestrator")
    engine.notes.put(notes_id, "## Goal\nship\n", if_rev=1, by="user")
    updated = [e.data for e in events if e.event == "notes.updated"]
    assert [(u["rev"], u["by"]) for u in updated] == [(1, "orchestrator"), (2, "user")]
    assert updated[-1] == {
        "worktree_path": feat,
        "agent_id": agent.agent_id,
        "notes_id": notes_id,
        "rev": 2,
        "size": len("## Goal\nship\n"),
        "by": "user",
    }


async def test_notes_are_written_off_the_loop_thread(world):
    engine = world.engine()
    loop_thread = threading.get_ident()
    threads = []
    writer = engine.notes._writer

    def recording(*args):
        threads.append(threading.get_ident())
        writer(*args)

    engine.notes._writer = recording
    agent = await engine.spawn_agent(str(world.feat))
    _write(engine, str(world.feat), agent.agent_id, "x")
    await engine.notes.flush()
    assert threads and loop_thread not in threads


async def test_a_new_conversation_keeps_the_notes(world):
    """/clear (P4): Claude reports another conversation id; same slot, same
    file, the meta follows."""
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "use sqlite")
    _conversation(engine, feat, agent.agent_id, "conv-1")
    assert engine.notes.get(notes_id).meta.conversation_id == "conv-1"
    _conversation(engine, feat, agent.agent_id, "conv-2")
    assert engine.notes_id(feat, agent.agent_id) == notes_id
    notes = engine.notes.get(notes_id)
    assert notes.meta.conversation_id == "conv-2" and "- use sqlite" in notes.text
    assert engine.manifest.get(feat, agent.agent_id).conversation_id == "conv-2"


async def test_a_restart_and_restore_carry_the_notes(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "use sqlite")
    _conversation(first, feat, agent.agent_id, "conv-a1")
    text = first.notes.get(notes_id).text
    await first.stop()  # engine stop: kept

    second = world.engine()
    assert second.notes.get(notes_id).meta.state == RESTORABLE
    [entry] = second.list_restorable()
    assert entry.notes_id == notes_id
    result = await second.restore_agents()
    [restored] = result["restored"]
    assert restored["agent_id"] == agent.agent_id
    assert second.notes_id(feat, agent.agent_id) == notes_id
    notes = second.notes.get(notes_id)
    assert notes.text == text and notes.meta.state == LIVE
    assert second.manifest.get(feat, agent.agent_id).notes_id == notes_id


async def test_a_restore_into_a_taken_agent_id_carries_the_notes(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "keep me")
    await first.stop()

    second = world.engine()
    [entry] = second.list_restorable()
    taken = await second.spawn_agent(
        feat, agent_id=agent.agent_id, resume_mode=ResumeMode.RESUME_LAST,
        resume_conversation="other",
    )
    assert taken.agent_id == agent.agent_id
    [restored] = (await second.restore_agents([entry.id]))["restored"]
    assert restored["agent_id"] != agent.agent_id
    assert second.notes_id(feat, restored["agent_id"]) == notes_id
    meta = second.notes.get(notes_id).meta
    assert meta.agent_id == restored["agent_id"] and "- keep me" in second.notes.get(notes_id).text
    assert second.notes_id(feat, agent.agent_id) != notes_id


async def test_a_crash_keeps_the_notes(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "x")
    await first.manifest.flush()
    await first.notes.flush()
    first.manifest = first.notes = None  # nothing more is written
    second = world.engine()
    assert [e.notes_id for e in second.list_restorable()] == [notes_id]
    assert second.notes.get(notes_id).meta.state == RESTORABLE


async def test_stop_deletes_the_notes_and_returns_them(world):
    engine = world.engine()
    events = _events(engine)
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "handoff")
    info, text = engine.stop_agent_with_notes(feat, agent.agent_id)
    assert info.agent_id == agent.agent_id
    assert "- handoff" in text
    assert notes_id not in engine.notes
    assert _removed(events, notes_id) == ["deleted"]
    await engine.notes.flush()
    assert not engine.notes.path(notes_id).exists()

    empty = await engine.spawn_agent(feat)
    assert engine.stop_agent_with_notes(feat, empty.agent_id)[1] is None
    with pytest.raises(ValueError, match="'delete' or 'keep'"):
        engine.stop_agent(feat, "a9", notes="archive")


async def test_stop_keeping_the_notes_leaves_an_orphan(world):
    engine = world.engine()
    events = _events(engine)
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "for later")
    engine.stop_agent(feat, agent.agent_id, notes="keep")
    meta = engine.notes.get(notes_id).meta
    assert meta.state == ORPHAN and meta.ended_at is not None
    assert _removed(events, notes_id) == ["orphaned"]
    await engine.stop()
    again = world.engine()
    assert again.notes.get(notes_id).meta.state == ORPHAN  # 7 days, not a restore
    assert again.list_restorable() == []


async def test_an_agent_that_exits_on_its_own_leaves_an_orphan(world):
    engine = world.engine()
    events = _events(engine)
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "half done")
    session = engine.get_session(feat, agent.agent_id)
    await _until(lambda: "your turn>" in session.screen_text())
    await engine.send_agent_input(feat, agent.agent_id, "exit 3")
    await _until(lambda: engine.get_session(feat, agent.agent_id) is None)
    assert engine.notes.get(notes_id).meta.state == ORPHAN
    assert _removed(events, notes_id) == ["orphaned"]


async def test_an_empty_orphan_is_not_kept(world):
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = engine.notes_id(feat, agent.agent_id)
    engine.stop_agent(feat, agent.agent_id, notes="keep")
    assert notes_id not in engine.notes


async def test_only_the_newest_restorable_notes_of_a_worktree_survive(world):
    first = world.engine()
    feat = str(world.feat)
    older = await first.spawn_agent(feat)
    newer = await first.spawn_agent(feat)
    old_id = _write(first, feat, older.agent_id, "old")
    new_id = _write(first, feat, newer.agent_id, "new")
    other = await first.spawn_agent(str(world.repo))
    other_id = _write(first, str(world.repo), other.agent_id, "main")
    await first.stop()

    second = world.engine()
    assert len(second.list_restorable()) == 3  # the entries stay restorable
    assert old_id not in second.notes
    assert new_id in second.notes and other_id in second.notes
    await second.notes.flush()
    assert not second.notes.path(old_id).exists()


async def test_a_fresh_spawn_deletes_the_worktrees_ended_notes(world):
    first = world.engine()
    feat = str(world.feat)
    restorable = await first.spawn_agent(feat)
    restorable_id = _write(first, feat, restorable.agent_id, "a")
    other = await first.spawn_agent(str(world.repo))
    other_id = _write(first, str(world.repo), other.agent_id, "b")
    await first.stop()

    second = world.engine()
    live = await second.spawn_agent(feat)
    live_id = _write(second, feat, live.agent_id, "live")
    orphan = await second.spawn_agent(feat, resume_mode=ResumeMode.RESUME_LAST,
                                      resume_conversation="conv-x")
    orphan_id = _write(second, feat, orphan.agent_id, "c")
    second.stop_agent(feat, orphan.agent_id, notes="keep")
    assert restorable_id not in second.notes  # the first spawn dropped it
    assert orphan_id in second.notes

    fresh = await second.spawn_agent(feat)
    assert orphan_id not in second.notes
    assert live_id in second.notes  # live agents keep theirs
    assert other_id in second.notes  # another worktree
    assert second.notes_id(feat, fresh.agent_id) in second.notes


async def test_a_continue_that_consumes_an_entry_orphans_its_notes(world):
    """Q31 + V5: the --continue agent runs the entry's conversation but gets
    new notes; the entry's are kept as an orphan, to adopt by hand."""
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "earlier")
    _conversation(first, feat, agent.agent_id, "conv-a1")
    await first.stop()

    second = world.engine()
    live = await second.spawn_agent(feat, resume_mode=ResumeMode.RESUME_LAST)
    assert second.list_restorable() == []
    _conversation(second, feat, live.agent_id, "conv-a1")  # consumed now
    assert second.notes_id(feat, live.agent_id) != notes_id
    meta = second.notes.get(notes_id).meta
    assert meta.state == ORPHAN and "- earlier" in second.notes.get(notes_id).text


async def test_dismiss_deletes_the_notes(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "x")
    await first.stop()
    second = world.engine()
    events = _events(second)
    [entry] = second.list_restorable()
    second.dismiss_restorable([entry.id])
    assert notes_id not in second.notes
    assert _removed(events, notes_id) == ["deleted"]


async def test_removing_the_worktree_deletes_its_notes(world):
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "x")
    engine.stop_agent(feat, agent.agent_id, notes="keep")
    await engine.remove_worktree(feat)
    assert notes_id not in engine.notes


async def test_notes_of_a_worktree_gone_meanwhile_are_pruned(world):
    first = world.engine()
    agent = await first.spawn_agent(str(world.feat))
    notes_id = _write(first, str(world.feat), agent.agent_id, "x")
    await first.stop()
    world.worktrees = world.worktrees[:1]
    second = world.engine()
    assert notes_id not in second.notes
    await second.notes.flush()
    assert not second.notes.path(notes_id).exists()


async def test_notes_without_an_entry_are_pruned_and_orphans_expire(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    stale_id = _write(first, feat, agent.agent_id, "x")
    await first.notes.flush()
    first.manifest = None  # its entry is never written
    first.notes = None

    orphaned = world.engine()  # no entry for stale_id: no right to stay
    assert stale_id not in orphaned.notes
    kept = await orphaned.spawn_agent(feat)
    kept_id = _write(orphaned, feat, kept.agent_id, "y")
    orphaned.stop_agent(feat, kept.agent_id, notes="keep")
    clock = orphaned.notes._clock
    orphaned.notes._clock = lambda: clock() + 8 * 86400
    orphaned._notes_gc()  # the hourly sweep
    assert kept_id not in orphaned.notes


async def test_the_orchestrators_notes_follow_the_same_rules(world):
    """Q-N9: one slot, the repo root as its worktree."""
    first = world.engine()
    await first.spawn_agent("", kind="orchestrator")
    notes_id = _write(first, ORCHESTRATOR_KEY, "", "plan")
    assert first.notes.get(notes_id).meta.kind == "orchestrator"
    await first.stop()
    second = world.engine()
    await second.restore_agents()
    assert second.notes_id(ORCHESTRATOR_KEY, "") == notes_id
    _, text = second.stop_agent_with_notes(ORCHESTRATOR_KEY, "")
    assert "- plan" in text and notes_id not in second.notes


async def test_a_failed_restore_keeps_the_notes(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "x")
    await first.stop()
    second = world.engine()

    def broken(name):
        raise KeyError(name)

    second._provider = broken
    result = await second.restore_agents()
    assert result["restored"] == []
    assert second.notes.get(notes_id).meta.state == RESTORABLE


async def test_without_a_manifest_notes_live_in_the_engines_own_dir(tmp_path):
    """Q-N4: a second engine in the repo (no lock, no manifest)."""
    from tests.engine.test_restore import _provider

    repo = tmp_path / "repo"
    repo.mkdir()
    engine = make_engine([worktree(str(repo))], repo_root=str(repo), provider=_provider(tmp_path))
    try:
        agent = await engine.spawn_agent(str(repo))
        notes_id = _write(engine, str(repo), agent.agent_id, "x")
        assert engine.notes.root == nt.notes_dir(str(repo)) / f"engine-{engine.instance_id[:12]}"
        _, text = engine.stop_agent_with_notes(str(repo), agent.agent_id)
        assert "- x" in text and notes_id not in engine.notes
        await asyncio.sleep(0)
    finally:
        await engine.stop()


async def test_notes_disabled(world):
    from lazyagent.config import Config, NotesConfig

    engine = world.engine(Config(notes=NotesConfig(enabled=False)))
    agent = await engine.spawn_agent(str(world.feat))
    assert engine.notes is None
    assert engine.manifest.get(str(world.feat), agent.agent_id).notes_id is None
    assert engine.stop_agent_with_notes(str(world.feat), agent.agent_id)[1] is None
    await engine.stop()
    assert mf.read(world.manifest) == ([], [])


# --- the per-spawn token (N2) ---------------------------------------------


def _token(engine: Engine, key: str, agent_id: str) -> str:
    [token] = [t for t, slot in engine._tokens.items() if slot == (key, agent_id)]
    return token


async def test_a_token_resolves_to_its_agent_and_dies_with_it(world):
    engine = world.engine()
    feat = str(world.feat)
    first = await engine.spawn_agent(feat)
    second = await engine.spawn_agent(feat)
    orchestrator = await engine.spawn_agent("", kind="orchestrator")
    tokens = {
        info.agent_id or "orch": _token(engine, info.worktree_path, info.agent_id)
        for info in (first, second, orchestrator)
    }
    assert len(set(tokens.values())) == 3
    assert engine.token_slot(tokens[first.agent_id]) == (feat, first.agent_id)
    assert engine.token_slot(tokens["orch"]) == (ORCHESTRATOR_KEY, "")
    assert engine.token_slot("forged") is None

    engine.stop_agent(feat, first.agent_id)  # a stop
    assert engine.token_slot(tokens[first.agent_id]) is None
    session = engine.get_session(feat, second.agent_id)
    await _until(lambda: "your turn>" in session.screen_text())
    await engine.send_agent_input(feat, second.agent_id, "exit 0")  # its own exit
    await _until(lambda: engine.get_session(feat, second.agent_id) is None)
    assert engine.token_slot(tokens[second.agent_id]) is None
    assert engine.token_slot(tokens["orch"]) == (ORCHESTRATOR_KEY, "")


async def test_a_restored_agent_gets_a_new_token(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    old = _token(first, feat, agent.agent_id)
    await first.stop()
    assert first._tokens == {}

    second = world.engine()
    [restored] = (await second.restore_agents())["restored"]
    assert restored["agent_id"] == agent.agent_id
    new = _token(second, feat, agent.agent_id)
    assert new != old and second.token_slot(old) is None
    assert second.token_slot(new) == (feat, agent.agent_id)


# --- the hooks (N3) --------------------------------------------------------


def _hook_line(engine: Engine, key: str, agent_id: str, **data) -> None:
    """What Claude's log hook appends, picked up on the next poll."""
    log = hooks_dir(engine.get_session(key, agent_id)) / "hook-events.jsonl"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(data) + "\n")
    engine.poll_agents()


def _notes_hook(engine: Engine, key: str, agent_id: str, payload: dict) -> str:
    """Run the agent's copy of notes_hook.py as Claude would: its own
    executable, the env the agent got."""
    session = engine.get_session(key, agent_id)
    [path] = re.findall(rf"{NOTES_PATH_ENV}=(\S+)", session.command)
    result = subprocess.run(
        [str(hooks_dir(session) / "notes_hook.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env={**os.environ, NOTES_PATH_ENV: shlex.split(path)[0]},
        timeout=10,
        check=True,
    )
    return result.stdout.decode()


async def test_the_first_prompt_is_pinned_and_survives_a_restore(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat, "Build the importer")
    notes_id = first.notes_id(feat, agent.agent_id)
    assert first.notes.get(notes_id).meta.instruction is None  # Claude reports it
    _hook_line(
        first, feat, agent.agent_id,
        hook_event_name="SessionStart", source="startup", session_id="conv-1",
    )
    assert first.notes.get(notes_id).meta.conversation_id == "conv-1"
    _hook_line(
        first, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="Build the importer, typed",
    )
    _hook_line(
        first, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="and now the tests",
    )
    assert first.notes.get(notes_id).meta.instruction == "Build the importer, typed"
    await first.stop()

    second = world.engine()
    await second.restore_agents()
    _hook_line(
        second, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="carry on",
    )
    assert second.notes.get(notes_id).meta.instruction == "Build the importer, typed"
    await second.notes.flush()
    meta = json.loads(second.notes.path(notes_id).with_suffix(".json").read_text())
    assert meta["instruction"] == "Build the importer, typed"


async def test_a_resumed_conversations_first_prompt_is_not_pinned(world):
    """Q-N24: after --continue the first prompt is not the original one."""
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat, resume_mode=ResumeMode.RESUME_LAST)
    _hook_line(
        engine, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="carry on",
    )
    notes_id = engine.notes_id(feat, agent.agent_id)
    assert engine.notes.get(notes_id).meta.instruction is None


async def test_a_pinned_instruction_alone_survives_a_restart(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = first.notes_id(feat, agent.agent_id)
    _hook_line(
        first, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="Build the importer",
    )
    await first.stop()
    second = world.engine()
    await second.restore_agents()
    assert second.notes_id(feat, agent.agent_id) == notes_id
    assert second.notes.get(notes_id).meta.instruction == "Build the importer"


async def test_without_hooks_the_spawn_instruction_is_pinned(tmp_path):
    """Q-N8: a provider whose hooks do not report the first prompt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    engine = make_engine(
        [worktree(str(repo))], repo_root=str(repo), provider=fake_agent_provider(tmp_path)
    )
    try:
        agent = await engine.spawn_agent(str(repo), "hello there")
        meta = engine.notes.get(engine.notes_id(str(repo), agent.agent_id)).meta
        assert meta.instruction == "hello there"
        assert NOTES_PATH_ENV not in engine.get_session(str(repo), agent.agent_id).command
    finally:
        await engine.stop()


async def test_the_agents_hook_reads_the_engines_notes(world):
    """The acceptance: a SessionStart compact payload prints the notes the
    engine wrote; a user's edit is noticed on the next prompt, once."""
    engine = world.engine()
    feat = str(world.feat)
    agent = await engine.spawn_agent(feat)
    notes_id = _write(engine, feat, agent.agent_id, "use sqlite")
    _hook_line(
        engine, feat, agent.agent_id, hook_event_name="UserPromptSubmit",
        session_id="conv-1", prompt="Build the importer",
    )
    await engine.notes.flush()
    out = json.loads(
        _notes_hook(
            engine, feat, agent.agent_id,
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": "c"},
        )
    )["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "by agent" in out["additionalContext"]
    assert "Original instruction (pinned):\nBuild the importer" in out["additionalContext"]
    assert "## Decisions\n- use sqlite" in out["additionalContext"]

    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "go", "session_id": "c"}
    assert _notes_hook(engine, feat, agent.agent_id, prompt) == ""
    rev = engine.notes.get(notes_id).meta.rev
    engine.notes.append(notes_id, "Constraints (don't touch)", "the CLI", by="user")
    assert engine.notes.get(notes_id).meta.rev == rev + 1
    await engine.notes.flush()
    out = json.loads(_notes_hook(engine, feat, agent.agent_id, prompt))
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "updated by user" in context and "+- the CLI" in context
    assert _notes_hook(engine, feat, agent.agent_id, prompt) == ""

    # Stopped: the hook's sidecar goes with the notes.
    seen = engine.notes.path(notes_id).with_name(f"{notes_id}.seen.json")
    assert seen.exists()
    engine.stop_agent(feat, agent.agent_id)
    await engine.notes.flush()
    assert not seen.exists()


async def test_a_restored_agent_gets_the_same_notes_path(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    notes_id = _write(first, feat, agent.agent_id, "use sqlite")
    path = str(first.notes.path(notes_id))
    assert f"{NOTES_PATH_ENV}={path}" in first.get_session(feat, agent.agent_id).command
    await first.stop()

    second = world.engine()
    [restored] = (await second.restore_agents())["restored"]
    session = second.get_session(feat, restored["agent_id"])
    assert f"{NOTES_PATH_ENV}={path}" in session.command


async def test_a_command_that_cannot_be_built_keeps_no_live_notes(world):
    engine = world.engine()
    feat = str(world.feat)

    def broken(*args, **kwargs):
        raise OSError("no temp dir")

    engine._agent_command = broken
    with pytest.raises(OSError):
        await engine.spawn_agent(feat)
    assert engine._notes_ids == {}
    assert engine.notes.list() == []  # a template: pruned as empty (Q-N15)
