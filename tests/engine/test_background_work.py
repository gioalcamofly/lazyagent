"""An agent whose turn ended with its own background work running will be
woken: it has not finished and it is not the user's turn. Read from what
Claude's Stop hook lists (``background_tasks``, ``session_crons``)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.agent_observers import BACKGROUND_STALE_SECONDS, ClaudeHooksObserver
from lazyagent.config import Config, InboxConfig, OrchestratorConfig
from lazyagent.engine.orchestrator import ORCHESTRATOR_KEY
from lazyagent.engine.session import AgentSession, StatusChanged
from lazyagent.models import AgentState, AgentStatus, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree
from tests.engine.test_home_polish import FEAT, KEY, NOW, Clock, _orchestrator, _pass, _row

PROMPT = {"hook_event_name": "UserPromptSubmit", "prompt": "Build it"}
TOOL = {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
IDLE = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}
APPROVAL = {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
SHELL = {"id": "b1", "type": "shell", "status": "running", "description": "Watch the checks"}
# Live updates of a published page: for the whole session, whatever the work.
WATCHER = {"id": "s1", "type": "monitor", "status": "running", "description": "live updates"}
TICK = {"hook_event_name": "UserPromptSubmit",
        "prompt": "<task-notification>\n<task-id>b1</task-id>\n<event>tick</event>"}
ENDED = {"hook_event_name": "UserPromptSubmit",
         "prompt": "<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>"}
LOOP = {"id": "c1", "schedule": "*/30 * * * *", "recurring": True, "prompt": "/check things"}
WAKEUP = {"id": "c2", "schedule": "5 10 5 11 *", "recurring": False, "prompt": "go on"}


def stop(tasks=(), crons=(), message="Armed the watch."):
    return {"hook_event_name": "Stop", "last_assistant_message": message,
            "background_tasks": list(tasks), "session_crons": list(crons)}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


class Hooks:
    """A Claude observer fed one hook at a time, on a clock of its own."""

    def __init__(self, tmp_path, name="hooks") -> None:
        self.path = tmp_path / f"{name}.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.now = 1000.0
        self.observer = ClaudeHooksObserver(str(self.path), prompted=True, clock=lambda: self.now)

    def write(self, *hooks: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(h) + "\n" for h in hooks)

    def feed(self, *hooks: dict):
        self.write(*hooks)
        return self.observer.poll()


# --- The observer ------------------------------------------------------------


class TestObserver:
    def test_a_stop_with_a_shell_running_is_not_a_finished_turn(self, tmp_path):
        hooks = Hooks(tmp_path)
        [_, event] = hooks.feed(PROMPT, stop([SHELL]))
        assert (event.status, event.detail) == (
            AgentStatus.RUNNING, "background work: Watch the checks")
        assert event.turned is True
        assert hooks.observer.waiting_on_background

    def test_several_tasks_are_counted(self, tmp_path):
        [_, event] = Hooks(tmp_path).feed(PROMPT, stop([SHELL, {**SHELL, "id": "b2"}]))
        assert event.detail == "background work: 2 tasks"

    def test_a_subagent_counts_and_a_finished_task_does_not(self, tmp_path):
        [_, event] = Hooks(tmp_path, "a").feed(PROMPT, stop([{**SHELL, "type": "subagent"}]))
        assert event.status == AgentStatus.RUNNING
        [_, event] = Hooks(tmp_path, "b").feed(PROMPT, stop([{**SHELL, "status": "completed"}]))
        assert event.status == AgentStatus.WAITING

    def test_a_session_long_watcher_is_not_work_being_waited_on(self, tmp_path):
        """It never ends: counting it would leave the agent working forever."""
        hooks = Hooks(tmp_path)
        [_, event] = hooks.feed(PROMPT, stop([WATCHER]))
        assert (event.status, event.detail) == (AgentStatus.WAITING, "waiting for input")
        assert not hooks.observer.waiting_on_background

    @pytest.mark.parametrize("hook", [
        {"hook_event_name": "Stop", "last_assistant_message": "Done."},
        stop(),
        {**stop(), "background_tasks": "not a list", "session_crons": 3},
        {**stop(), "background_tasks": ["junk"], "session_crons": ["junk"]},
    ])
    def test_no_background_work_reads_as_before(self, tmp_path, hook):
        [_, event] = Hooks(tmp_path).feed(PROMPT, hook)
        assert (event.status, event.detail, event.question) == (
            AgentStatus.WAITING, "waiting for input", "")

    def test_claudes_idle_reminder_does_not_end_the_wait(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        [event] = hooks.feed(IDLE)
        assert event.status == AgentStatus.RUNNING
        assert hooks.observer.waiting_on_background

    def test_a_wake_up_that_ends_with_work_left_is_the_same_wait(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        events = hooks.feed(TICK, stop([SHELL]))
        assert [e.status for e in events] == [AgentStatus.RUNNING, AgentStatus.RUNNING]
        assert hooks.observer.background_runs == 1

    def test_the_last_wake_up_ends_the_turn(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        [_, event] = hooks.feed(ENDED, stop())
        assert event.status == AgentStatus.WAITING
        assert not hooks.observer.waiting_on_background
        assert event.background_runs == 1

    def test_a_turn_that_ends_in_a_question_goes_to_the_user(self, tmp_path):
        hooks = Hooks(tmp_path)
        [_, event] = hooks.feed(PROMPT, stop([SHELL], message="Should I merge it?"))
        assert event.status == AgentStatus.WAITING
        assert event.question == "Should I merge it?"
        assert not hooks.observer.waiting_on_background

    @pytest.mark.parametrize("hook, status", [
        (APPROVAL, AgentStatus.WAITING_FOR_APPROVAL),
        ({"hook_event_name": "Notification", "notification_type": "elicitation_dialog"},
         AgentStatus.WAITING_FOR_USER),
    ])
    def test_an_approval_meanwhile_still_reaches_the_user(self, tmp_path, hook, status):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        [event] = hooks.feed(hook)
        assert event.status == status
        hooks.now += BACKGROUND_STALE_SECONDS + 1
        assert hooks.observer.poll() == [], "staleness does not take it from the user"

    def test_a_subagents_tool_after_an_approval_goes_back_to_the_wait(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([{**SHELL, "type": "subagent"}]), APPROVAL)
        [event] = hooks.feed({**TOOL, "hook_event_name": "PostToolUse", "agent_id": "sub1"})
        assert event.status == AgentStatus.RUNNING
        assert event.detail.startswith("background work")

    def test_a_stale_signal_falls_back_once(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        hooks.now += BACKGROUND_STALE_SECONDS - 1
        assert hooks.observer.poll() == []
        hooks.now += 1
        [event] = hooks.observer.poll()
        assert (event.status, event.detail, event.turned) == (
            AgentStatus.WAITING, "waiting for input", True)
        assert not hooks.observer.waiting_on_background
        assert hooks.observer.poll() == []
        assert hooks.feed(IDLE)[0].status == AgentStatus.WAITING

    def test_each_stop_starts_the_staleness_again(self, tmp_path):
        """A Monitor re-armed every 30 minutes for hours."""
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        for _ in range(8):
            hooks.now += 1800
            assert [e.status for e in hooks.feed(TICK, stop([SHELL]))] == [
                AgentStatus.RUNNING] * 2

    @pytest.mark.parametrize("hook", [
        {"hook_event_name": "SessionEnd"},
        {"hook_event_name": "SessionStart", "source": "clear"},
    ])
    def test_a_session_that_ended_or_cleared_waits_on_nothing(self, tmp_path, hook):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        hooks.feed(hook)
        assert not hooks.observer.waiting_on_background

    def test_a_list_task_completed_does_not_end_the_wait(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop([SHELL]))
        assert hooks.feed({"hook_event_name": "TaskCompleted"}) == []

    def test_a_recurring_cron_leaves_a_turn_someone_asked_for_alone(self, tmp_path):
        [_, event] = Hooks(tmp_path).feed(PROMPT, stop(crons=[LOOP]))
        assert event.status == AgentStatus.WAITING

    def test_a_tick_of_its_cron_is_a_self_woken_turn(self, tmp_path):
        hooks = Hooks(tmp_path)
        hooks.feed(PROMPT, stop(crons=[LOOP]))
        [_, event] = hooks.feed({**PROMPT, "prompt": "/check things"}, stop(crons=[LOOP]))
        assert (event.status, event.detail) == (
            AgentStatus.RUNNING, "waiting for its next wake-up")
        [_, event] = hooks.feed(PROMPT, stop(crons=[LOOP]))
        assert event.status == AgentStatus.WAITING, "the user wrote: their turn again"

    def test_a_clipped_cron_prompt_still_matches(self, tmp_path):
        hooks = Hooks(tmp_path)
        clipped = {**LOOP, "prompt": "/check… [+990 chars]"}
        hooks.feed(PROMPT, stop(crons=[clipped]))
        [_, event] = hooks.feed({**PROMPT, "prompt": "/check things"}, stop(crons=[clipped]))
        assert event.status == AgentStatus.RUNNING

    def test_a_one_shot_wake_up_counts_until_it_fired(self, tmp_path):
        hooks = Hooks(tmp_path)
        [_, event] = hooks.feed(PROMPT, stop(crons=[WAKEUP]))
        assert event.status == AgentStatus.RUNNING
        [_, event] = hooks.feed({**PROMPT, "prompt": "go on"}, stop())
        assert event.status == AgentStatus.WAITING


class TestSession:
    def _session(self, hooks):
        session = AgentSession("unused", worktree_path=FEAT, agent_id="a1",
                               observer=hooks.observer)
        session._on_text("Welcome")
        session.last_output_time = 0.0  # long quiet
        return session

    def test_the_hang_check_leaves_the_wait_alone_then_applies(self, tmp_path):
        hooks = Hooks(tmp_path)
        session = self._session(hooks)
        hooks.write(PROMPT, stop([SHELL]))
        session.check_hang(1.0)
        assert session.status == AgentStatus.RUNNING
        hooks.write(TICK)
        session.check_hang(1.0)
        assert session.status == AgentStatus.POSSIBLY_HANGED

    def test_a_dead_process_is_not_waiting_any_more(self, tmp_path):
        hooks = Hooks(tmp_path)
        session = self._session(hooks)
        hooks.write(PROMPT, stop([SHELL]))
        session.poll()
        session._on_disconnect()
        assert not hooks.observer.waiting_on_background
        session.check_hang(1.0)
        assert session.status == AgentStatus.POSSIBLY_HANGED

    def test_the_stale_fallback_reaches_the_session(self, tmp_path):
        hooks = Hooks(tmp_path)
        session = self._session(hooks)
        hooks.write(PROMPT, stop([SHELL]))
        session.poll()
        hooks.now += BACKGROUND_STALE_SECONDS
        session.poll()
        assert session.status == AgentStatus.WAITING

    def test_a_detail_or_a_run_reaches_the_listeners(self, tmp_path):
        hooks = Hooks(tmp_path)
        session = self._session(hooks)
        seen = []
        session.add_listener(seen.append)
        hooks.write(PROMPT, stop([SHELL]), TICK)
        session.poll()
        assert seen[-2].detail.startswith("background work")
        assert seen[-1].background_runs == 1


# --- The engine --------------------------------------------------------------


def _engine(**config):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
        config=Config(**config),
    )
    engine.status.clock = Clock()
    return engine


def _session(engine, agent_id="a1"):
    session = MagicMock()
    session.worktree_path, session.agent_id = FEAT, agent_id
    session.running = True
    session.send_input = AsyncMock()
    add_session(engine, session)
    return engine.get_agent(FEAT, agent_id)


def _apply(engine, info, events):
    for e in events:
        engine._on_agent_event(info, StatusChanged(
            FEAT, info.agent_id, e.status, e.confidence, e.detail, None, e.question, e.turned,
            e.background_runs, e.background))


def _kinds(engine):
    return [i["kind"] for i in engine.actionables.list()["items"]]


def _woken(orch):
    return [c.args[0] for c in orch.send_input.await_args_list]


def _worker(tmp_path):
    engine = _engine(orchestrator=OrchestratorConfig())
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    return engine, orch, _session(engine), Hooks(tmp_path)


async def _not_your_turn(engine, orch):
    await _pass(engine)
    row = _row(engine)
    assert "done" not in _kinds(engine)
    assert row["hold"] is None and row["ball"] != "you"
    assert _woken(orch) == []


@pytest.mark.asyncio
async def test_a_stop_with_a_shell_opens_no_item_and_no_hold(tmp_path):
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL])))
    await _not_your_turn(engine, orch)
    assert engine.agent_states(FEAT)["a1"].detail == "background work: Watch the checks"
    _apply(engine, info, hooks.feed(IDLE))
    await _not_your_turn(engine, orch)


@pytest.mark.asyncio
async def test_self_woken_turns_open_nothing(tmp_path):
    """A Monitor that ticks and is re-armed all morning."""
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL])))
    await _not_your_turn(engine, orch)
    for _ in range(7):
        _apply(engine, info, hooks.feed(TICK))
        await _not_your_turn(engine, orch)
        _apply(engine, info, hooks.feed(TOOL, stop([SHELL])))
        await _not_your_turn(engine, orch)


@pytest.mark.asyncio
async def test_a_cron_tick_opens_nothing(tmp_path):
    """The turn that set the loop up is a finished one; its ticks are not."""
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop(crons=[LOOP])))
    await _pass(engine)
    assert "done" in _kinds(engine)
    assert len(_woken(orch)) == 1
    for _ in range(3):
        _apply(engine, info, hooks.feed({**PROMPT, "prompt": "/check things"}, stop(crons=[LOOP])))
        await _pass(engine)
        assert "done" not in _kinds(engine)
        assert len(_woken(orch)) == 1


@pytest.mark.asyncio
async def test_when_the_work_ends_and_the_turn_ends_done_and_hold_fire_once(tmp_path):
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL])))
    await _pass(engine)
    _apply(engine, info, hooks.feed(TICK, stop([SHELL])))
    await _pass(engine)
    _apply(engine, info, hooks.feed(ENDED, stop(message="All green.")))
    await _pass(engine)
    assert "done" in _kinds(engine)
    assert _row(engine)["hold"]["auto"] is True
    [wake] = _woken(orch)
    assert wake.startswith(f"Item {KEY}: agent a1 (spawned by you) — agent finished")
    await _pass(engine)
    _apply(engine, info, hooks.feed(IDLE))
    await _pass(engine)
    assert len(_woken(orch)) == 1, "exactly once"


@pytest.mark.asyncio
async def test_a_report_outlives_the_wake_ups_and_ends_with_the_real_finish(tmp_path):
    """Asked to say so, an agent reports before ending its turn: the note
    stays through the wake-ups, and does not hold back the finish."""
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT))
    await _pass(engine)

    def report():
        engine.overlay.report(
            KEY, FEAT, "a1", note="waiting for CI", ball="others", needs_user=False,
            label="", now=engine.status.clock(), until=None,
            turn=engine.status.turn(FEAT, "a1"))

    report()
    for wake in (None, TICK, TICK):
        hooks.write(*([wake] if wake else []), stop([SHELL]))
        _apply(engine, info, hooks.observer.poll())
        await _pass(engine)
        assert engine.overlay.reports[KEY]["ball"] == "others"
        assert engine.overlay.reports[KEY]["note"] == "waiting for CI"
        assert _woken(orch) == []
    _apply(engine, info, hooks.feed(ENDED, stop(message="All green.")))
    await _pass(engine)
    await _pass(engine)
    assert engine.overlay.reports[KEY]["ball"] is None
    assert "done" in _kinds(engine)
    assert len(_woken(orch)) == 1


@pytest.mark.asyncio
async def test_a_report_made_in_the_last_turn_still_holds(tmp_path):
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL]), ENDED))
    await _pass(engine)
    engine.overlay.report(
        KEY, FEAT, "a1", note="waiting for QA", ball="others", needs_user=False,
        label="", now=engine.status.clock(), until=None, turn=engine.status.turn(FEAT, "a1"))
    _apply(engine, info, hooks.feed(stop()))
    await _pass(engine)
    assert engine.overlay.reports[KEY]["ball"] == "others"
    assert _woken(orch) == []


@pytest.mark.asyncio
async def test_a_question_with_background_work_reaches_the_user(tmp_path):
    engine, _, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL], message="Should I merge it?")))
    await _pass(engine)
    assert engine.agent_states(FEAT)["a1"].question == "Should I merge it?"
    assert "done" not in _kinds(engine)
    assert "turn_question" in _kinds(engine)


@pytest.mark.asyncio
async def test_a_permission_prompt_with_background_work_reaches_the_user(tmp_path):
    engine, _, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([{**SHELL, "type": "subagent"}]), APPROVAL))
    await _pass(engine)
    assert engine.agent_states(FEAT)["a1"].status == AgentStatus.WAITING_FOR_APPROVAL
    assert "approval" in _kinds(engine)


@pytest.mark.asyncio
async def test_a_stale_signal_falls_back_to_done_and_hold(tmp_path):
    engine, orch, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL])))
    await _not_your_turn(engine, orch)
    hooks.now += BACKGROUND_STALE_SECONDS
    _apply(engine, info, hooks.observer.poll())
    await _pass(engine)
    assert "done" in _kinds(engine)
    assert len(_woken(orch)) == 1


@pytest.mark.asyncio
async def test_auto_delegation_spends_one_attempt_across_a_long_background_wait(
        monkeypatch, tmp_path):
    engine = _engine(inbox=InboxConfig(auto_delegate=["conflict_with_base"]))
    engine.autodelegate.teammate = AsyncMock(return_value=False)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        7, "Feat", "OPEN", [], mergeable="CONFLICTING")
    spawned = []

    async def spawn(path, instruction=None, **kw):
        spawned.append(instruction)
        return _session(engine, f"a{len(spawned)}")

    monkeypatch.setattr(engine, "spawn_agent", spawn)
    await _pass(engine)
    assert len(spawned) == 1
    info = engine.get_agent(FEAT, "a1")
    hooks = Hooks(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT, stop([SHELL])))
    for _ in range(3):
        await _pass(engine)
    for _ in range(3):
        _apply(engine, info, hooks.feed(TICK, stop([SHELL])))
        await _pass(engine)
        await _pass(engine)
    assert engine.autodelegate.attempts[(KEY, "conflict")]["count"] == 1
    assert len(spawned) == 1
    # The wait over with the conflict still there: that attempt failed.
    _apply(engine, info, hooks.feed(ENDED, stop()))
    await _pass(engine)
    await _pass(engine)
    assert engine.autodelegate.attempts[(KEY, "conflict")]["count"] == 2


# --- Input reaches an agent that waits on its background work ----------------


def test_a_worker_on_its_background_work_is_free_for_a_letter(tmp_path):
    engine, _, info, hooks = _worker(tmp_path)
    _apply(engine, info, hooks.feed(PROMPT))
    letters = engine.first_responder.mailbox
    assert letters.ready((FEAT, "a1")) is False, "in its turn: not yet"
    _apply(engine, info, hooks.feed(stop([SHELL])))
    assert letters.ready((FEAT, "a1")) is True
    _apply(engine, info, hooks.feed(APPROVAL))
    assert letters.ready((FEAT, "a1")) is False, "an approval is up"
    _apply(engine, info, hooks.feed(TICK))
    assert letters.ready((FEAT, "a1")) is False, "woken: in its turn again"


def test_the_orchestrator_on_its_background_work_can_be_woken(tmp_path):
    engine, _, _, _ = _worker(tmp_path)
    states = engine.registry.states.setdefault(ORCHESTRATOR_KEY, {})
    states["o1"] = AgentState(status=AgentStatus.RUNNING, background=True)
    assert engine.claims._orchestrator_idle() is True
    states["o1"] = AgentState(status=AgentStatus.RUNNING)
    assert engine.claims._orchestrator_idle() is False
    states["o1"] = AgentState(status=AgentStatus.WAITING_FOR_APPROVAL, background=True)
    assert engine.claims._orchestrator_idle() is False
