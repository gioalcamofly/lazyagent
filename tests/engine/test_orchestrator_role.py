"""The orchestrator as an authenticated role (proposal 0013 §5c, I1): only
the orchestrator's token writes as the orchestrator, and the spawns agents
ask for are capped by the engine."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.engine.methods import EngineMethods, classify
from lazyagent.engine.notes import NotesStore
from tests.engine.fakes import make_engine, no_pty, worktree

pytestmark = pytest.mark.asyncio

FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"


@pytest.fixture
def engine(monkeypatch, tmp_path):
    no_pty(monkeypatch)
    engine = make_engine(
        [
            worktree("/tmp/repo"),
            worktree(FEAT, "feat", is_main=False),
            worktree(FIX, "fix", is_main=False),
        ],
        config=Config(orchestrator=OrchestratorConfig(max_concurrent_agents=2)),
    )
    engine.notes = NotesStore(tmp_path / "notes")
    return engine


def _token(engine, key: str, agent_id: str) -> str:
    return next(t for t, slot in engine._tokens.items() if slot == (key, agent_id))


async def _spawn(methods, path=FEAT, **params):
    return await methods.spawn_agent({"worktree_path": path, **params})


# --- Who writes notes -------------------------------------------------------


async def test_a_call_with_no_token_writes_as_the_user_not_the_orchestrator(engine):
    methods = EngineMethods(engine)
    worker = await _spawn(methods)
    target = {"worktree_path": FEAT, "agent_id": worker["agent_id"]}

    written = await methods.notes_append({**target, "section": "Doubts", "text": "db?"})
    assert written["by"] == "user"
    assert engine.notes.get(written["notes_id"]).meta.by == "user"


async def test_only_the_orchestrators_token_writes_as_the_orchestrator(engine):
    methods = EngineMethods(engine)
    worker = await _spawn(methods)
    other = await _spawn(methods, FIX)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    target = {"worktree_path": FEAT, "agent_id": worker["agent_id"]}

    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])
    written = await methods.notes_append(
        {**target, "section": "Constraints (don't touch)", "text": "no migrations",
         "caller": caller}
    )
    assert written["by"] == "orchestrator"

    own = await methods.notes_append(
        {"section": "Decisions", "text": "sqlite",
         "caller": _token(engine, FEAT, worker["agent_id"])}
    )
    assert (own["agent_id"], own["by"]) == (worker["agent_id"], "agent")

    with pytest.raises(ValueError, match="only write its own notes"):
        await methods.notes_append(
            {**target, "section": "Decisions", "text": "x",
             "caller": _token(engine, FIX, other["agent_id"])}
        )
    with pytest.raises(ValueError, match="Unknown caller token"):
        await methods.notes_append(
            {**target, "section": "Decisions", "text": "x", "caller": "forged"}
        )


# --- Spawn caps ---------------------------------------------------------------


async def test_an_agent_cannot_spawn_over_max_concurrent_agents(engine):
    methods = EngineMethods(engine)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])

    # The orchestrator itself does not count; two workers fill the cap.
    await _spawn(methods, caller=caller)
    await _spawn(methods, FIX, caller=caller)
    with pytest.raises(ValueError) as refused:
        await _spawn(methods, caller=caller)
    code, message, _ = classify(refused.value)
    assert code == "VALIDATION_ERROR"
    assert "max_concurrent_agents = 2" in message and "2 agents are running" in message
    assert len(engine.agent_ids(FEAT)) == 1  # nothing was started

    # The user is not capped.
    await _spawn(methods)
    assert len(engine.agent_ids(FEAT)) == 2

    # A worker's token is capped too.
    worker = _token(engine, FIX, engine.agent_ids(FIX)[0])
    with pytest.raises(ValueError, match="max_concurrent_agents"):
        await _spawn(methods, caller=worker)

    engine.stop_agent(FEAT, engine.agent_ids(FEAT)[0])
    engine.stop_agent(FEAT, engine.agent_ids(FEAT)[0])
    await _spawn(methods, caller=caller)


async def test_an_agent_cannot_spawn_over_max_spawns_per_day(engine):
    engine.config = Config(
        orchestrator=OrchestratorConfig(max_spawns_per_day=2, max_concurrent_agents=0)
    )
    clock = [1_700_000_000.0]
    engine.spawn_caps.clock = lambda: clock[0]
    methods = EngineMethods(engine)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])

    for _ in range(2):
        await _spawn(methods, caller=caller)
        engine.stop_agent(FEAT, engine.agent_ids(FEAT)[0])
    with pytest.raises(ValueError, match=r"max_spawns_per_day = 2"):
        await _spawn(methods, caller=caller)
    await _spawn(methods)  # the user's spawns are not counted nor refused

    clock[0] += 86400
    await _spawn(methods, caller=caller)


async def test_a_failed_spawn_gives_its_place_back(engine, monkeypatch):
    engine.config = Config(
        orchestrator=OrchestratorConfig(max_spawns_per_day=1, max_concurrent_agents=1)
    )
    methods = EngineMethods(engine)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])

    async def broken(*args, **kwargs):
        raise RuntimeError("provider exploded")

    real = engine.spawn_agent
    monkeypatch.setattr(engine, "spawn_agent", broken)
    with pytest.raises(RuntimeError):
        await _spawn(methods, caller=caller)
    monkeypatch.setattr(engine, "spawn_agent", real)
    await _spawn(methods, caller=caller)


async def test_concurrent_spawns_cannot_both_slip_under_the_cap(engine, monkeypatch):
    engine.config = Config(orchestrator=OrchestratorConfig(max_concurrent_agents=1))
    methods = EngineMethods(engine)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])

    real = engine.spawn_agent

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await real(*args, **kwargs)

    monkeypatch.setattr(engine, "spawn_agent", slow)
    results = await asyncio.gather(
        _spawn(methods, caller=caller),
        _spawn(methods, FIX, caller=caller),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ValueError) for r in results) == 1
    assert len(engine.agent_ids(FEAT)) + len(engine.agent_ids(FIX)) == 1


async def test_zero_turns_a_cap_off(engine):
    engine.config = Config(
        orchestrator=OrchestratorConfig(max_spawns_per_day=0, max_concurrent_agents=0)
    )
    methods = EngineMethods(engine)
    orchestrator = await methods.spawn_agent({"kind": "orchestrator"})
    caller = _token(engine, "__orchestrator__", orchestrator["agent_id"])
    for _ in range(12):
        await _spawn(methods, caller=caller)
    assert len(engine.agent_ids(FEAT)) == 12

