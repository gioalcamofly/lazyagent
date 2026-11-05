"""An agent that has had no turn yet has finished nothing: a spawn with an
instruction is starting until its first prompt, and a restore (or a spawn
with no instruction) at its prompt opens no "Your turn" item, no hold for
the orchestrator, and no failed auto-delegation attempt."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.agent_observers import ClaudeHooksObserver
from lazyagent.config import Config, InboxConfig, OrchestratorConfig
from lazyagent.engine.rules.facts import FACTS, RowContext
from lazyagent.engine.session import AgentSession, StatusChanged
from lazyagent.models import AgentState, AgentStatus, LifecycleConfidence, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree
from tests.engine.test_home_polish import FEAT, KEY, NOW, Clock, _orchestrator, _pass, _row

START = {"hook_event_name": "SessionStart", "source": "startup"}
RESUME = {"hook_event_name": "SessionStart", "source": "resume"}
PROMPT = {"hook_event_name": "UserPromptSubmit", "prompt": "Build it"}
TOOL = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
STOP = {"hook_event_name": "Stop", "last_assistant_message": "Done."}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


class Hooks:
    """A Claude observer fed one hook at a time, as its log grows."""

    def __init__(self, tmp_path, prompted: bool) -> None:
        self.path = tmp_path / "hooks.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.observer = ClaudeHooksObserver(str(self.path), prompted=prompted)

    def write(self, *hooks: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(h) + "\n" for h in hooks)

    def feed(self, *hooks: dict):
        self.write(*hooks)
        return self.observer.poll()


# --- The observer ------------------------------------------------------------


class TestObserver:
    def test_a_spawn_with_an_instruction_is_starting_until_its_prompt(self, tmp_path):
        hooks = Hooks(tmp_path, prompted=True)
        [start] = hooks.feed(START)
        assert (start.status, start.detail, start.turned) == (
            AgentStatus.RUNNING, "starting", False)
        [prompt] = hooks.feed(PROMPT)
        assert (prompt.status, prompt.turned) == (AgentStatus.RUNNING, True)
        [stop] = hooks.feed(STOP)
        assert (stop.status, stop.turned) == (AgentStatus.WAITING, True)

    @pytest.mark.parametrize("hook", [START, RESUME])
    def test_a_bare_spawn_or_a_restore_waits_at_its_prompt_with_no_turn(self, tmp_path, hook):
        hooks = Hooks(tmp_path, prompted=False)
        [event] = hooks.feed(hook)
        assert (event.status, event.detail, event.turned) == (
            AgentStatus.WAITING, "at its prompt", False)
        assert hooks.feed(PROMPT)[0].turned is True

    def test_a_restore_is_at_its_prompt_even_if_once_prompted(self, tmp_path):
        """A later SessionStart (``/clear``) after a turn is at its prompt."""
        hooks = Hooks(tmp_path, prompted=True)
        hooks.feed(START, PROMPT, STOP)
        [event] = hooks.feed({"hook_event_name": "SessionStart", "source": "clear"})
        assert (event.status, event.turned) == (AgentStatus.WAITING, True)

    def test_a_subagents_prompt_is_not_the_agents_turn(self, tmp_path):
        hooks = Hooks(tmp_path, prompted=False)
        hooks.feed(RESUME, {**PROMPT, "agent_id": "sub1"})
        assert hooks.observer.turned is False

    def test_each_event_says_whether_a_turn_had_happened_by_then(self, tmp_path):
        """One poll that reads the whole start: the SessionStart event is
        still before the turn."""
        hooks = Hooks(tmp_path, prompted=False)
        events = hooks.feed(START, PROMPT, STOP)
        assert [e.turned for e in events] == [False, True, True]

    def test_the_session_reports_running_not_waiting_on_a_prompted_start(self, tmp_path):
        hooks = Hooks(tmp_path, prompted=True)
        session = AgentSession("unused", worktree_path=FEAT, agent_id="a1",
                               observer=hooks.observer)
        seen = []
        session.add_listener(seen.append)
        session._on_text("Welcome")
        hooks.write(START)
        session.poll()
        assert session.status == AgentStatus.RUNNING and not session.turned
        hooks.write(PROMPT, STOP)
        session.poll()
        assert session.status == AgentStatus.WAITING and session.turned
        assert AgentStatus.WAITING not in [e.status for e in seen[:-1]]


# --- The engine: no "Your turn", no hold, before a turn ----------------------


def _engine(**config):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
        config=Config(**config),
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _session(engine, agent_id="a1"):
    session = MagicMock()
    session.worktree_path, session.agent_id = FEAT, agent_id
    session.running = True
    session.send_input = AsyncMock()
    add_session(engine, session)
    return engine.get_agent(FEAT, agent_id)


def _apply(engine, info, events):
    """What the engine hears from the session for these observer events."""
    for e in events:
        engine._on_agent_event(info, StatusChanged(
            FEAT, info.agent_id, e.status, e.confidence, e.detail, None, e.question, e.turned))


def _kinds(engine):
    return [i["kind"] for i in engine.actionables.list()["items"]]


def _woken(orch):
    return [c.args[0] for c in orch.send_input.await_args_list]


async def _start(engine, info):
    # session.start(): RUNNING before any hook.
    engine._on_agent_event(info, StatusChanged(
        FEAT, info.agent_id, AgentStatus.RUNNING, LifecycleConfidence.LOW, "", None, "", False))
    await _pass(engine)


@pytest.mark.asyncio
async def test_a_spawn_with_an_instruction_has_no_done_row_nor_hold_until_its_first_stop(
        tmp_path):
    engine, _ = _engine(orchestrator=OrchestratorConfig())
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    info = _session(engine)
    hooks = Hooks(tmp_path, prompted=True)
    await _start(engine, info)
    for hook in (START, PROMPT, TOOL):
        _apply(engine, info, hooks.feed(hook))
        await _pass(engine)
        row = _row(engine)
        assert "done" not in _kinds(engine)
        assert row["hold"] is None and row["ball"] == "agent"
    assert _woken(orch) == []
    _apply(engine, info, hooks.feed(STOP))
    await _pass(engine)
    assert "done" in _kinds(engine)
    assert _row(engine)["hold"]["auto"] is True
    [wake] = _woken(orch)
    assert wake.startswith(f"Item {KEY}: agent a1 (spawned by you) — agent finished")


@pytest.mark.asyncio
@pytest.mark.parametrize("hook", [RESUME, START])
async def test_a_restore_or_a_bare_spawn_at_its_prompt_opens_no_item(tmp_path, hook):
    engine, _ = _engine(orchestrator=OrchestratorConfig())
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    info = _session(engine)
    await _start(engine, info)
    _apply(engine, info, Hooks(tmp_path, prompted=False).feed(hook))
    await _pass(engine)
    assert engine.agent_states(FEAT)["a1"].status == AgentStatus.WAITING, "still idle"
    items = [i for i in engine.actionables.list()["items"] if i["work_item"] == KEY]
    assert items == []
    row = _row(engine)
    assert row["hold"] is None and row["ball"] != "you"
    assert _woken(orch) == []


@pytest.mark.asyncio
async def test_without_the_queue_an_agent_with_no_turn_is_not_finished():
    engine, clock = _engine()
    _session(engine)
    engine.registry.states.setdefault(FEAT, {})["a1"] = AgentState(
        status=AgentStatus.WAITING, agent_id="a1", turned=False)
    engine.status.flush()
    facts = engine.status.facts(KEY)
    assert FACTS["agents.idle"].row(RowContext(facts, clock.now, clock.now, 24)) == 0
    engine.registry.states[FEAT]["a1"].turned = True
    engine.status.flush()
    facts = engine.status.facts(KEY)
    assert FACTS["agents.idle"].row(RowContext(facts, clock.now, clock.now, 24)) == 1


@pytest.mark.asyncio
async def test_the_auto_hold_waits_for_a_real_turn_even_with_a_users_done_rule():
    """A user rule's done may not read agent.turned: the hold still does."""
    engine, _ = _engine(orchestrator=OrchestratorConfig())
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    _session(engine)
    engine.registry.states.setdefault(FEAT, {})["a1"] = AgentState(
        status=AgentStatus.WAITING, agent_id="a1", turned=False)
    item = {"kind": "done", "work_item": KEY, "occurrence": "o1", "agent_id": "a1",
            "worktree_path": FEAT, "id": "Done:x", "state": "review: agent finished"}
    engine.claims.auto_hold([item], NOW)
    assert engine.claims.holds == {}
    engine.registry.states[FEAT]["a1"].turned = True
    engine.claims.auto_hold([item], NOW)
    assert KEY in engine.claims.holds
    assert orch is not None


# --- Auto-delegation across a slow start --------------------------------------


@pytest.mark.asyncio
async def test_auto_delegation_spends_one_attempt_across_a_slow_start(monkeypatch, tmp_path):
    engine, _ = _engine(inbox=InboxConfig(auto_delegate=["conflict_with_base"]))
    engine.autodelegate.teammate = AsyncMock(return_value=False)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        7, "Feat", "OPEN", [], mergeable="CONFLICTING")
    spawned = []
    hooks = {}

    async def spawn(path, instruction=None, **kw):
        agent_id = f"a{len(spawned) + 1}"
        spawned.append((agent_id, instruction))
        info = _session(engine, agent_id)
        hooks[agent_id] = Hooks(tmp_path / agent_id, prompted=True)
        engine._on_agent_event(info, StatusChanged(
            FEAT, agent_id, AgentStatus.RUNNING, LifecycleConfidence.LOW, "", None, "", False))
        return info

    for agent_id in ("a1", "a2"):
        (tmp_path / agent_id).mkdir()
    monkeypatch.setattr(engine, "spawn_agent", spawn)
    await _pass(engine)
    assert [a for a, _ in spawned] == ["a1"]
    info = engine.get_agent(FEAT, "a1")
    # A slow start: seconds before its first prompt, a while at the start.
    _apply(engine, info, hooks["a1"].feed(START))
    for _ in range(3):
        await _pass(engine)
    # Even a status that said "at its prompt" before any turn is no failure.
    engine.registry.states[FEAT]["a1"].status = AgentStatus.WAITING
    for _ in range(3):
        await _pass(engine)
    attempt = engine.autodelegate.attempts[(KEY, "conflict")]
    assert (attempt["count"], len(spawned)) == (1, 1), "one attempt, the brief sent once"
    assert [i for i in engine.decisions.list() if i.get("source") == "auto_delegate"] == []
    # Its real turn ends with the conflict still there: that one failed.
    _apply(engine, info, hooks["a1"].feed(PROMPT, TOOL, STOP))
    await _pass(engine)
    await _pass(engine)
    assert engine.autodelegate.attempts[(KEY, "conflict")]["count"] == 2
    texts = [t for _, t in spawned] + [
        c.args[0] for c in engine.get_session(FEAT, "a1").send_input.await_args_list]
    assert len(texts) == len(set(texts)), "never the same brief twice"
