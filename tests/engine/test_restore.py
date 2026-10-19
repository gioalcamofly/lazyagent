"""The restart manifest through the engine: an engine records its agents,
the next one offers them and brings them back, each resuming its
conversation (real PTYs, the fake agent)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from lazyagent.agent_providers import AgentProvider, ResumeMode
from lazyagent.config import Config, EngineConfig
from lazyagent.engine import manifest as mf
from lazyagent.engine.engine import ORCHESTRATOR_KEY, Engine
from lazyagent.engine.events import EngineEvent
from lazyagent.protocol import validation
from tests.engine.fakes import fake_agent_provider, make_engine, worktree

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _provider(tmp_path: Path) -> AgentProvider:
    """The fake agent, with Claude's resume and session-name flags."""
    fake = fake_agent_provider(tmp_path)
    return AgentProvider(
        name="claude",
        executable=fake.executable,
        dangerous_flag="--paste",
        resume_pick_args=("--resume",),
        resume_last_args=("--continue",),
        session_name_flag="--name",
    )


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.feat = tmp_path / "repo-feat"
        self.repo.mkdir()
        self.feat.mkdir()
        self.manifest = tmp_path / "state" / "m.manifest.json"
        self.worktrees = [
            worktree(str(self.repo)),
            worktree(str(self.feat), "feat", is_main=False),
        ]
        self.engines: list[Engine] = []
        # Once: a crashed engine's agent may still be running the shim, and
        # rewriting a running executable fails (ETXTBSY).
        self.provider = _provider(tmp_path)

    def engine(self, config: Config | None = None) -> Engine:
        engine = make_engine(
            list(self.worktrees),
            repo_root=str(self.repo),
            provider=self.provider,
            config=config,
        )
        engine.open_manifest(self.manifest)
        self.engines.append(engine)
        return engine

    async def stop_all(self) -> None:
        for engine in self.engines:
            await engine.stop()


@pytest_asyncio.fixture
async def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        await w.stop_all()


def _events(engine: Engine) -> list[EngineEvent]:
    events: list[EngineEvent] = []

    def record(event: EngineEvent) -> None:
        validation.validate_event(
            event.event, json.loads(json.dumps(event.data)), strict=True
        )
        events.append(event)

    engine.subscribe(record)
    return events


async def test_a_restart_offers_and_restores_the_running_agents(world):
    first = world.engine()
    feat = str(world.feat)
    a1 = await first.spawn_agent(feat, "hello", label="Reviewer")
    await first.spawn_agent("", kind="orchestrator", skip_permissions=True)
    a2 = await first.spawn_agent(feat)
    shown = first.get_session(feat, a2.agent_id)
    await _until(lambda: "your turn>" in shown.screen_text())
    first.stop_agent(feat, a2.agent_id)  # stopped on purpose: not offered
    await first.send_agent_input(feat, a1.agent_id, "review the diff")
    # Claude's hooks report the conversation id; the engine notes it.
    first.get_session(feat, a1.agent_id)._observer.conversation_id = "conv-a1"
    first.poll_agents()
    await first.stop()  # the agents die with it; the manifest keeps them

    second = world.engine()
    events = _events(second)
    offered = {e.agent_id: e for e in second.list_restorable()}
    assert set(offered) == {a1.agent_id, ""}
    entry = offered[a1.agent_id]
    assert (entry.worktree_path, entry.kind) == (feat, "agent")
    assert entry.provider == "claude"
    assert entry.label == "Reviewer" and entry.session_name == a1.session_name
    assert entry.conversation_id == "conv-a1"
    assert entry.instruction == "review the diff"
    assert entry.engine_id == first.instance_id and entry.ended_at is not None
    assert offered[""].kind == "orchestrator" and offered[""].skip_permissions

    result = await second.restore_agents()
    assert result["failed"] == []
    restored = {r["agent_id"]: r for r in result["restored"]}
    assert restored[a1.agent_id]["resumed"] == "conversation"
    assert restored[""]["resumed"] == "last"
    assert [e.data for e in events if e.event == "engine.restored"] == [result]

    # Same agent id, label and session name; the exact conversation resumed.
    session = second.get_session(feat, a1.agent_id)
    assert session is not None
    assert "--resume conv-a1" in session.command
    assert f"--name {a1.session_name}" in session.command
    assert second.get_agent(feat, a1.agent_id).label == "Reviewer"
    orchestrator = second.get_session(ORCHESTRATOR_KEY, "")
    assert "--continue" in orchestrator.command and "--paste" in orchestrator.command
    await _until(lambda: "your turn>" in session.screen_text())
    assert second.list_restorable() == []
    # The restored agents are this engine's now: recorded again, with what
    # was known of them.
    again = second.manifest.get(feat, a1.agent_id)
    assert again.conversation_id == "conv-a1" and again.instruction == "review the diff"


async def test_a_crash_leaves_the_agents_restorable(world):
    """No stop at all (SIGKILL, power loss): what the debounced writer last
    wrote is what the next engine finds."""
    first = world.engine()
    agent = await first.spawn_agent(str(world.feat))
    await first.manifest.flush()
    on_disk, _ = mf.read(world.manifest)
    assert [e.agent_id for e in on_disk] == [agent.agent_id]
    first.manifest = None  # the crashed engine writes nothing more
    second = world.engine()
    assert [e.agent_id for e in second.list_restorable()] == [agent.agent_id]


async def test_exited_agents_are_not_offered(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    session = first.get_session(feat, agent.agent_id)
    await _until(lambda: "your turn>" in session.screen_text())
    await first.send_agent_input(feat, agent.agent_id, "exit 3")
    await _until(lambda: first.get_session(feat, agent.agent_id) is None)
    await first.stop()
    assert world.engine().list_restorable() == []


async def test_entries_of_a_removed_worktree_are_pruned(world):
    first = world.engine()
    await first.spawn_agent(str(world.feat))
    await first.stop()
    world.worktrees = world.worktrees[:1]  # repo-feat was removed meanwhile
    second = world.engine()
    assert second.list_restorable() == []
    await second.manifest.flush()
    assert mf.read(world.manifest) == ([], [])


async def test_a_failed_restore_stays_restorable(world):
    first = world.engine()
    await first.spawn_agent(str(world.feat))
    await first.stop()
    second = world.engine()
    [entry] = second.list_restorable()

    def broken(name):
        raise KeyError(name)

    second._provider = broken
    result = await second.restore_agents([entry.id, "nope"])
    assert result["restored"] == []
    reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "not restorable" in reasons["nope"]
    assert entry.id in reasons
    assert [e.id for e in second.list_restorable()] == [entry.id]


# --- Entries whose agent already runs here (not restored, but started again:
# --- the UIs launch the orchestrator with --continue) ------------------------


def _conversation(engine: Engine, key: str, agent_id: str, conversation: str):
    """What Claude's hooks report, noted on the next poll."""
    engine.get_session(key, agent_id)._observer.conversation_id = conversation
    engine.poll_agents()


def _changes(events: list[EngineEvent]) -> list[list[str]]:
    return [
        [a["id"] for a in e.data["agents"]]
        for e in events
        if e.event == "restorable.changed"
    ]


async def test_an_orchestrator_started_again_consumes_its_entry(world):
    """The bug: a new engine starts the orchestrator with --continue (the
    same conversation), and the old entry was still offered; restoring it
    would put a second Claude on one transcript."""
    first = world.engine()
    await first.spawn_agent("", kind="orchestrator")
    _conversation(first, ORCHESTRATOR_KEY, "", "186b11b6")
    await first.stop()

    second = world.engine()
    events = _events(second)
    [entry] = second.list_restorable()
    assert entry.kind == "orchestrator" and entry.conversation_id == "186b11b6"
    await second.spawn_agent(
        "", kind="orchestrator", resume_mode=ResumeMode.RESUME_LAST
    )
    assert second.list_restorable() == []
    assert _changes(events) == [[]]  # the UIs drop their banner
    # Consumed, not just hidden: gone from the file, and not offered when
    # this orchestrator stops.
    second.stop_agent(ORCHESTRATOR_KEY, "")
    assert second.list_restorable() == []
    await second.manifest.flush()
    assert mf.read(world.manifest) == ([], [])

    result = await second.restore_agents([entry.id])
    assert result["restored"] == []
    [failure] = result["failed"]
    assert failure["reason"] == "already running in this engine (the orchestrator)"


async def test_a_restore_of_a_live_orchestrator_is_refused(world):
    first = world.engine()
    await first.spawn_agent("", kind="orchestrator")
    await first.stop()
    second = world.engine()
    [entry] = second.list_restorable()
    live = await second.spawn_agent("", kind="orchestrator")
    result = await second.restore_agents([entry.id])
    assert result["restored"] == []
    [failure] = result["failed"]
    assert "already running in this engine" in failure["reason"]
    # Not spawned: the same orchestrator still runs.
    assert second.get_agent(ORCHESTRATOR_KEY, "").session_id == live.session_id
    assert second.list_restorable() == []


async def test_an_agent_resuming_the_conversation_consumes_its_entry(world):
    """--resume <id> of the entry's conversation (another agent id, even)."""
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    _conversation(first, feat, agent.agent_id, "conv-a1")
    await first.stop()

    second = world.engine()
    events = _events(second)
    [entry] = second.list_restorable()
    await second.spawn_agent(
        feat, resume_mode=ResumeMode.RESUME_LAST, resume_conversation="conv-a1"
    )
    assert second.list_restorable() == []
    assert _changes(events) == [[]]
    result = await second.restore_agents([entry.id])
    assert result["restored"] == []
    assert "already running in this engine (agent" in result["failed"][0]["reason"]


async def test_an_agent_continuing_in_its_slot_hides_then_consumes(world):
    """--continue in the same worktree: the conversation id comes later,
    from the hooks. Until then the entry is hidden and a restore refused;
    once it is known to be the same, the entry is consumed."""
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    _conversation(first, feat, agent.agent_id, "conv-a1")
    await first.stop()

    second = world.engine()
    events = _events(second)
    [entry] = second.list_restorable()
    live = await second.spawn_agent(
        feat, resume_mode=ResumeMode.RESUME_LAST, agent_id=agent.agent_id
    )
    assert live.agent_id == agent.agent_id
    assert second.list_restorable() == []
    result = await second.restore_agents([entry.id])
    assert result["restored"] == []
    assert "may be running in this engine" in result["failed"][0]["reason"]
    assert entry.id in second.manifest.restorable  # kept, for now

    _conversation(second, feat, live.agent_id, "conv-a1")
    assert entry.id not in second.manifest.restorable
    assert second.list_restorable() == []
    assert _changes(events) == [[]]


async def test_an_agent_continuing_another_conversation_shows_it_again(world):
    first = world.engine()
    feat = str(world.feat)
    agent = await first.spawn_agent(feat)
    _conversation(first, feat, agent.agent_id, "conv-a1")
    await first.stop()

    second = world.engine()
    events = _events(second)
    [entry] = second.list_restorable()
    live = await second.spawn_agent(feat, resume_mode=ResumeMode.RESUME_LAST)
    assert second.list_restorable() == []
    _conversation(second, feat, live.agent_id, "conv-other")
    assert [e.id for e in second.list_restorable()] == [entry.id]
    assert _changes(events) == [[], [entry.id]]


async def test_an_entry_with_no_conversation_id_and_a_continue_is_consumed(world):
    """Another provider (no hooks): both resume the worktree's last
    conversation, so they are the same one."""
    first = world.engine()
    feat = str(world.feat)
    await first.spawn_agent(feat)
    await first.stop()
    second = world.engine()
    [entry] = second.list_restorable()
    assert entry.conversation_id is None
    await second.spawn_agent(feat, resume_mode=ResumeMode.RESUME_LAST)
    assert second.list_restorable() == []
    assert entry.id not in second.manifest.restorable


async def test_agents_that_are_not_live_are_still_offered(world):
    """No over-filtering: a fresh agent in the same slot, a --continue in
    another worktree, an agent on another conversation and a live
    orchestrator leave the agent entries offered."""
    first = world.engine()
    feat, repo = str(world.feat), str(world.repo)
    one = await first.spawn_agent(feat)
    _conversation(first, feat, one.agent_id, "conv-one")
    await first.spawn_agent(feat)  # no conversation id known
    await first.stop()

    second = world.engine()
    events = _events(second)
    offered = sorted(e.id for e in second.list_restorable())
    assert len(offered) == 2
    await second.spawn_agent(feat, agent_id=one.agent_id)  # fresh: a new one
    other = await second.spawn_agent(feat)
    _conversation(second, feat, other.agent_id, "conv-two")
    await second.spawn_agent(repo, resume_mode=ResumeMode.RESUME_LAST)
    await second.spawn_agent("", kind="orchestrator", resume_mode=ResumeMode.RESUME_LAST)
    assert sorted(e.id for e in second.list_restorable()) == offered
    assert _changes(events) == []
    result = await second.restore_agents()
    assert result["failed"] == [] and len(result["restored"]) == 2


async def test_one_restore_never_brings_a_conversation_back_twice(world):
    """Two entries of one conversation (another slot each): the first is
    restored, the second refused."""
    first = world.engine()
    feat = str(world.feat)
    a = await first.spawn_agent(feat)
    b = await first.spawn_agent(feat)
    _conversation(first, feat, a.agent_id, "conv-same")
    _conversation(first, feat, b.agent_id, "conv-same")
    await first.stop()
    second = world.engine()
    assert len(second.list_restorable()) == 2
    result = await second.restore_agents()
    assert len(result["restored"]) == 1
    [failure] = result["failed"]
    assert "already running in this engine" in failure["reason"]
    assert second.list_restorable() == []


async def test_dismiss(world):
    first = world.engine()
    await first.spawn_agent(str(world.feat))
    await first.spawn_agent(str(world.feat))
    await first.stop()
    second = world.engine()
    one, two = second.list_restorable()
    assert second.dismiss_restorable([one.id, "nope"]) == [one.id]
    assert second.dismiss_restorable() == [two.id]
    assert second.list_restorable() == []


async def test_a_reused_agent_id_in_use_gets_a_new_one(world):
    first = world.engine()
    agent = await first.spawn_agent(str(world.feat))
    await first.stop()
    second = world.engine()
    [entry] = second.list_restorable()
    # Same id taken meanwhile (ids are per engine here: no id store).
    taken = await second.spawn_agent(str(world.feat), agent_id=agent.agent_id)
    assert taken.agent_id == agent.agent_id
    result = await second.restore_agents([entry.id])
    [restored] = result["restored"]
    assert restored["agent_id"] != agent.agent_id


async def test_restore_on_start(world):
    first = world.engine()
    agent = await first.spawn_agent(str(world.feat))
    await first.stop()
    config = Config(engine=EngineConfig(restore_on_start=True))
    second = world.engine(config)
    await second.start(ipc=False)
    feat = str(world.feat)
    await _until(lambda: second.get_session(feat, agent.agent_id) is not None)
    assert second.list_restorable() == []


async def test_without_a_manifest_the_methods_say_so(tmp_path):
    engine = make_engine([worktree(str(tmp_path))], repo_root=str(tmp_path))
    with pytest.raises(ValueError, match="no restart manifest"):
        engine.list_restorable()
    with pytest.raises(ValueError, match="no restart manifest"):
        await engine.restore_agents()


async def test_the_engine_that_holds_the_lock_keeps_the_manifest(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    first = make_engine([worktree(str(repo))], repo_root=str(repo))
    first._persist_manifest = True
    second = make_engine([worktree(str(repo))], repo_root=str(repo))
    second._persist_manifest = True
    await first.start()
    try:
        await second.start()
        assert first.manifest is not None
        assert first.manifest.path == mf.manifest_path(str(repo))
        assert second.manifest is None  # lost the lock: hands off the file
        await second.stop()
    finally:
        await first.stop()
