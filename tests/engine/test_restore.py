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

from lazyagent.agent_providers import AgentProvider
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

    def engine(self, config: Config | None = None) -> Engine:
        engine = make_engine(
            list(self.worktrees),
            repo_root=str(self.repo),
            provider=_provider(self.tmp),
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
    await first.spawn_agent("", kind="orchestrator")
    await first.stop()
    second = world.engine()
    [entry] = second.list_restorable()
    await second.spawn_agent("", kind="orchestrator")  # one runs already
    result = await second.restore_agents([entry.id, "nope"])
    assert result["restored"] == []
    reasons = {f["id"]: f["reason"] for f in result["failed"]}
    assert "already running" in reasons[entry.id]
    assert "not restorable" in reasons["nope"]
    assert [e.id for e in second.list_restorable()] == [entry.id]


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
