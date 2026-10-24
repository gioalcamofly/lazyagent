"""The orchestrator as first responder (proposal 0013 §5c, I4b): routing,
presence and mandate, caps, the escalation timer, log and revert, the
never-alone refusal."""

from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.engine import orchestrator as orch
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.notes import NotesStore
from lazyagent.engine.orchestrator import DECISION_ANSWERED
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
FIX = "/tmp/repo-fix"
KEY = "repo:feat"

OPTIONS = [
    {"key": "A", "label": "Exponential, cap 30 s"},
    {"key": "B", "label": "Fixed 2 s, 5 tries"},
]
BRIEF = {
    "question": "Retries: fixed or exponential backoff?",
    "options": OPTIONS,
    "recommendation": "A",
    "tried": "fixed 2 s: 3 of 30 runs hit the 10 s limit",
    "evidence": [{"kind": "url", "url": "https://example.com/run/1"}],
    "reversible": "yes",
    "impact_if_wrong": "slower failures",
    "class": "quick",
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(tmp_path=None, config: Config | None = None, *, ui: bool = True):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False),
         worktree(FIX, "fix", is_main=False)],
        config=config,
    )
    clock = Clock()
    engine.status.clock = clock
    engine.first_responder.presence.connected = lambda: ui
    if tmp_path is not None:
        engine.notes = NotesStore(tmp_path / "notes")
    # head_sha runs git: none here.
    engine.head_sha = AsyncMock(return_value=None)
    return engine, clock


def _agent(engine, path, agent_id, status, detail=""):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id, detail=detail
    )
    if agent_id not in engine.agent_ids(path):
        session = MagicMock()
        session.worktree_path, session.agent_id = path, agent_id
        session.running = True
        session.send_input = AsyncMock()
        add_session(engine, session, kind="orchestrator" if path == ORCHESTRATOR_KEY else "agent")
        engine._tokens["tok-orch" if path == ORCHESTRATOR_KEY else f"tok-{agent_id}"] = (
            path, agent_id,
        )
        if engine.notes is not None and path != ORCHESTRATOR_KEY:
            meta = engine.notes.create(path, agent_id)
            engine._notes_ids[(path, agent_id)] = meta.notes_id
    return engine.get_session(path, agent_id)


def _orchestrator(engine, status="waiting"):
    return _agent(engine, ORCHESTRATOR_KEY, "", status)


def _row(engine, key=KEY):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key)


async def _ask(engine, agent="a1", **changes):
    result = await EngineMethods(engine).request_decision(
        {**BRIEF, **changes, "caller": f"tok-{agent}"}
    )
    return result["decision"]


def _away(engine):
    engine.first_responder.presence.set("away")


def _present(engine):
    engine.first_responder.presence.set("present")


def _sent(session):
    return [c.args[0] for c in session.send_input.await_args_list]


# --- Routing -----------------------------------------------------------------


async def test_with_no_orchestrator_a_brief_is_the_users():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    assert record["holder"] == "user"
    assert _row(engine)["holder"] == "user"


async def test_a_routed_brief_wakes_the_orchestrator_once_its_turn_is_over():
    engine, _ = _engine()
    orchestrator = _orchestrator(engine, "running")
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    assert record["holder"] == "orchestrator"
    row = _row(engine)
    assert (row["zone"], row["state"], row["holder"]) == (
        "in_flight", "orchestrator · looking", "orchestrator",
    )
    assert engine.actionables.list()["items"] == [], "not the user's inbox"
    await engine.wait_idle()
    orchestrator.send_input.assert_not_called()

    _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    engine.status.flush()
    await engine.wait_idle()
    assert _sent(orchestrator) == [
        f"Item {KEY}: decision {record['id']}. Read it with status_list / decision_get."
    ]


async def test_away_the_orchestrator_answers_a_reversible_brief_and_the_user_can_revert(tmp_path):
    engine, clock = _engine(tmp_path)
    _orchestrator(engine)
    _away(engine)
    worker = _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine, **{"class": "deep"})
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.data["decision"]), {DECISION_ANSWERED})

    answered = (await EngineMethods(engine).decision_answer(
        {"id": record["id"], "choice": "a", "reason": "3/30 timeouts with fixed",
         "caller": "tok-orch"}
    ))["decision"]
    assert answered["state"] == "answered"
    assert (answered["answered_by"], answered["revertible"]) == ("orchestrator", True)
    assert answered["answer"]["choice"] == "A"
    assert answered["answer"]["reason"] == "3/30 timeouts with fixed"
    assert answered["answer"]["presence"] == "away"
    assert [d["id"] for d in seen] == [record["id"]]

    # Delivered like a review reply: only once the worker is idle.
    await engine.wait_idle()
    worker.send_input.assert_not_called()
    _agent(engine, FEAT, "a1", "waiting")
    engine.status.flush()
    await engine.wait_idle()
    assert _sent(worker) == [f"Decision {record['id']}: A — Exponential, cap 30 s."]
    assert engine.decisions.get(record["id"])["answer"]["delivered_at"] == clock.now

    notes = engine.notes.get(engine.notes_id(FEAT, "a1")).text
    assert "Retries: fixed or exponential backoff? → A (orchestrator, away): 3/30 timeouts" in notes
    [entry] = [e for e in engine.overlay.entries(KEY) if e.get("action") == "answer_reversible"]
    assert (entry["mandate"], entry["action"], entry["reason"]) == (
        "away", "answer_reversible", "3/30 timeouts with fixed",
    )
    assert engine.status.list()["counts"]["auto_actions"] == 1

    reverted = (await EngineMethods(engine).decision_revert(
        {"id": record["id"], "choice": "B", "comment": "keep it simple"}
    ))["decision"]
    assert reverted["revertible"] is False
    assert reverted["reverted"]["choice"] == "B"
    engine.status.flush()
    await engine.wait_idle()
    assert _sent(worker)[-1] == (
        f"Decision {record['id']} overridden by the user: B — Fixed 2 s, 5 tries. keep it simple"
    )
    counts = engine.status.list()["counts"]
    assert (counts["auto_actions"], counts["auto_reverted"]) == (1, 1)
    assert "(user, revert): keep it simple" in engine.notes.get(engine.notes_id(FEAT, "a1")).text
    with pytest.raises(ValueError, match="already reverted"):
        await EngineMethods(engine).decision_revert({"id": record["id"]})
    with pytest.raises(ValueError, match="Only the user"):
        await EngineMethods(engine).decision_revert({"id": record["id"], "caller": "tok-orch"})


async def test_a_brief_that_is_not_reversible_goes_to_the_user_at_once():
    engine, _ = _engine()
    _orchestrator(engine)
    _away(engine)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine, reversible="no")
    assert record["holder"] == "user"
    assert [i["decision_id"] for i in engine.actionables.list()["items"]] == [record["id"]]


async def test_an_unanswered_routed_brief_reaches_the_user_after_ten_minutes():
    engine, clock = _engine()
    _orchestrator(engine, "running")
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    clock.now += 599
    assert _row(engine)["holder"] == "orchestrator"
    clock.now += 1
    row = _row(engine)
    assert (row["zone"], row["holder"]) == ("inbox", "user")
    record = engine.decisions.get(record["id"])
    assert (record["holder"], record["escalation"]["note"], record["escalation"]["by"]) == (
        "user", "orchestrator did not answer", "engine",
    )
    [item] = engine.actionables.list()["items"]
    assert item["detail"].startswith("orchestrator did not answer")


async def test_the_orchestrator_escalates_with_a_suggestion():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine, **{"class": "deep"})
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="Only the orchestrator"):
        await methods.decision_escalate({"id": record["id"], "note": "x"})
    escalated = (await methods.decision_escalate(
        {"id": record["id"], "note": "product call", "suggestion": "b", "caller": "tok-orch"}
    ))["decision"]
    assert escalated["escalation"]["suggestion"] == "B"
    [item] = engine.actionables.list()["items"]
    assert (item["level"], item["detail"]) == ("deep", "orchestrator suggests B: product call")
    with pytest.raises(ValueError, match="the user's now"):
        await methods.decision_answer(
            {"id": record["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
        )


async def test_the_orchestrator_exiting_hands_its_items_to_the_user():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    engine._agents.pop(ORCHESTRATOR_KEY)
    engine.registry.states.pop(ORCHESTRATOR_KEY)
    assert _row(engine)["holder"] == "user"
    assert engine.decisions.get(record["id"])["escalation"]["note"] == "orchestrator gone"


async def test_keep_for_me_routes_nothing_to_the_orchestrator():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    methods = EngineMethods(engine)
    result = await methods.status_gesture({"key": KEY, "kind": "keep_for_me"})
    assert result["kept"] is True
    assert (await _ask(engine))["holder"] == "user"
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    assert (await _ask(engine))["holder"] == "orchestrator"


async def test_a_plain_question_is_routed_and_answered_with_free_text():
    engine, _ = _engine()
    orchestrator = _orchestrator(engine)
    _away(engine)
    worker = _agent(engine, FEAT, "a1", "waiting_for_user", "Continue with the migration?")
    row = _row(engine)
    assert (row["state"], row["holder"]) == ("orchestrator · looking", "orchestrator")
    [record] = engine.decisions.list()
    assert (record["source"], record["holder"], record["blocking"]) == (
        "question", "orchestrator", True,
    )
    await engine.wait_idle()
    assert _sent(orchestrator) == [
        f"Item {KEY}: question {record['id']}. Read it with status_list / decision_get."
    ]
    await EngineMethods(engine).decision_answer(
        {"id": record["id"], "choice": "yes, go on", "reason": "plan says so",
         "caller": "tok-orch"}
    )
    await engine.wait_idle()
    assert _sent(worker) == [f"Decision {record['id']}: yes, go on."]
    # It moves on: the question is not routed again.
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    assert len(engine.decisions.list()) == 1


async def test_a_question_answered_in_the_terminal_is_resolved():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "waiting_for_user", "Which DB?")
    engine.status.flush()
    [record] = engine.decisions.list()
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    assert engine.decisions.get(record["id"])["state"] == "resolved"
    # Not the orchestrator's any more: the working agent has it.
    assert _row(engine)["holder"] == "agent"


async def test_a_tool_approval_is_never_routed():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    row = _row(engine)
    assert (row["level"], row["holder"]) == ("now", "user")
    assert engine.decisions.list() == []


# --- Mandate -----------------------------------------------------------------


async def test_present_the_orchestrator_answers_only_small_reversible_things():
    engine, _ = _engine()
    _orchestrator(engine)
    _present(engine)
    _agent(engine, FEAT, "a1", "running")
    methods = EngineMethods(engine)
    deep = await _ask(engine, **{"class": "deep"})
    with pytest.raises(ValueError, match="Not within your mandate .*decision_escalate"):
        await methods.decision_answer(
            {"id": deep["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
        )
    partly = await _ask(engine, reversible="partly")
    with pytest.raises(ValueError, match="Not within your mandate"):
        await methods.decision_answer(
            {"id": partly["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
        )
    await methods.decision_withdraw({"id": deep["id"], "caller": "tok-a1"})
    quick = await _ask(engine)
    answered = await methods.decision_answer(
        {"id": quick["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
    )
    assert answered["decision"]["answer"]["presence"] == "present"


async def test_away_allow_only_shortens_the_mandate():
    config = Config(orchestrator=OrchestratorConfig(away_allow=["answer_quick"]))
    engine, _ = _engine(config=config)
    _orchestrator(engine)
    _away(engine)
    _agent(engine, FEAT, "a1", "running")
    deep = await _ask(engine, **{"class": "deep"})
    with pytest.raises(ValueError, match="Not within your mandate"):
        await EngineMethods(engine).decision_answer(
            {"id": deep["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
        )
    assert engine.first_responder.verdict("merge_default_branch", KEY)[0] == "refuse"


async def test_the_answer_needs_a_reason_and_a_known_option():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="reason is required"):
        await methods.decision_answer({"id": record["id"], "choice": "A", "caller": "tok-orch"})
    with pytest.raises(ValueError, match="choice must be one of A, B"):
        await methods.decision_answer(
            {"id": record["id"], "choice": "C", "reason": "r", "caller": "tok-orch"}
        )
    with pytest.raises(ValueError, match="A worker does not answer"):
        await methods.decision_answer({"id": record["id"], "choice": "A", "caller": "tok-a1"})


async def test_the_user_answers_through_the_same_path(tmp_path):
    engine, _ = _engine(tmp_path)
    worker = _agent(engine, FEAT, "a1", "waiting")
    record = await _ask(engine)
    answered = (await EngineMethods(engine).decision_answer(
        {"id": record["id"], "choice": "B", "comment": "simpler"}
    ))["decision"]
    assert (answered["answered_by"], answered["revertible"]) == ("user", False)
    await engine.wait_idle()
    assert _sent(worker) == [f"Decision {record['id']}: B — Fixed 2 s, 5 tries. simpler"]
    assert "→ B (user): simpler" in engine.notes.get(engine.notes_id(FEAT, "a1")).text


async def test_a_spawn_on_a_held_item_follows_the_mandate_and_revert_stops_it(monkeypatch):
    engine, _ = _engine()
    _orchestrator(engine)
    _present(engine)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    methods = EngineMethods(engine)

    async def spawn(path, instruction=None, **kw):
        _agent(engine, path, "a9", "running")
        return engine.get_agent(path, "a9")

    monkeypatch.setattr(engine, "spawn_agent", spawn)
    with pytest.raises(ValueError, match="Not within your mandate \\(present\\): propose"):
        await methods.spawn_agent({"worktree_path": FEAT, "caller": "tok-orch"})
    # On an item it does not hold, it acts on the user's word (caps only).
    await methods.spawn_agent({"worktree_path": FIX, "caller": "tok-orch"})
    engine._agents[FIX].pop("a9")

    _away(engine)
    await methods.decision_answer(
        {"id": record["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
    )
    # Held items are answered: hold one again for the spawn.
    second = await _ask(engine)
    await methods.spawn_agent({"worktree_path": FEAT, "caller": "tok-orch"})
    assert engine.decisions.get(record["id"])["inverse"] == [
        {"kind": "stop_agent", "worktree_path": FEAT, "agent_id": "a9"}
    ]
    assert second["holder"] == "orchestrator"
    reverted = await methods.decision_revert({"id": record["id"]})
    assert reverted["decision"]["reverted"]["results"] == ["stopped agent a9"]
    assert "a9" not in engine.agent_ids(FEAT)


# --- Caps ----------------------------------------------------------------------


async def test_past_the_per_item_cap_items_go_to_the_user_to_sit_down_with():
    engine, _ = _engine()
    _orchestrator(engine)
    _away(engine)
    _agent(engine, FEAT, "a1", "running")
    methods = EngineMethods(engine)
    for _ in range(3):
        record = await _ask(engine)
        await methods.decision_answer(
            {"id": record["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
        )
    capped = await _ask(engine)
    assert (capped["holder"], capped["class"]) == ("user", "deep")
    assert "3 orchestrator decisions on this item today" in capped["capped"]
    [item] = engine.actionables.list()["items"]
    assert item["level"] == "deep"


async def test_past_the_daily_cap_and_the_next_day():
    config = Config(orchestrator=OrchestratorConfig(max_decisions_day=1))
    engine, clock = _engine(config=config)
    _orchestrator(engine)
    _away(engine)
    _agent(engine, FEAT, "a1", "running")
    _agent(engine, FIX, "a2", "running")
    methods = EngineMethods(engine)
    record = await _ask(engine)
    await methods.decision_answer(
        {"id": record["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
    )
    other = await _ask(engine, agent="a2")
    assert other["holder"] == "user" and "1 orchestrator decisions today" in other["capped"]
    clock.now += 86400
    assert (await _ask(engine, agent="a2"))["holder"] == "orchestrator"
    assert engine.status.list()["counts"]["auto_actions"] == 0


# --- Presence --------------------------------------------------------------------


async def test_presence_follows_input_reports_and_the_override():
    engine, clock = _engine()
    methods = EngineMethods(engine)
    presence = engine.status.list()["presence"]
    assert (presence["mode"], presence["effective"]) == ("auto", "away")

    result = await methods.presence_report({"input_at": clock.now - 60})
    assert result["presence"]["effective"] == "present"
    assert result["presence"]["since"] == clock.now
    clock.now += 14 * 60 - 1
    assert engine.status.list()["presence"]["effective"] == "present"
    clock.now += 60
    assert engine.status.list()["presence"]["effective"] == "away"

    assert (await methods.presence_set({"mode": "present"}))["presence"]["effective"] == "present"
    clock.now += 3600
    assert engine.status.list()["presence"]["effective"] == "present"
    await methods.presence_set({"mode": "auto"})
    assert engine.status.list()["presence"]["effective"] == "away"
    with pytest.raises(ValueError, match="mode must be one of"):
        await methods.presence_set({"mode": "busy"})
    with pytest.raises(ValueError, match="input_at must be"):
        await methods.presence_report({"input_at": "now"})


async def test_no_ui_connected_is_away_whatever_the_override():
    engine, clock = _engine(ui=False)
    methods = EngineMethods(engine)
    await methods.presence_report({"input_at": clock.now})
    await methods.presence_set({"mode": "present"})
    assert engine.status.list()["presence"]["effective"] == "away"


async def test_presence_is_the_users_only():
    engine, _ = _engine()
    _orchestrator(engine)
    with pytest.raises(ValueError, match="the user's"):
        await EngineMethods(engine).presence_set({"mode": "away", "caller": "tok-orch"})


# --- Never alone -------------------------------------------------------------------


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def git_env(monkeypatch):
    for name, value in {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)


async def test_the_orchestrator_may_not_remove_a_worktree_with_unpushed_work(tmp_path, git_env):
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    assert await orch.unpushed(str(repo)) == "1 commits on no remote"
    (repo / "f").write_text("x")
    assert await orch.unpushed(str(repo)) == "uncommitted changes"

    engine, _ = _engine()
    _orchestrator(engine)
    engine.world.worktrees.append(worktree(str(repo), "wt", is_main=False))
    engine.refresh_worktrees()
    with pytest.raises(ValueError, match="Never alone: deleting a worktree with unpushed work"):
        await EngineMethods(engine).remove_worktree(
            {"worktree_path": str(repo), "caller": "tok-orch"}
        )
    # The user may.
    await EngineMethods(engine).remove_worktree({"worktree_path": str(repo)})


async def test_revert_resets_an_unpushed_branch_and_skips_a_pushed_one(tmp_path, git_env):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feat")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()

    engine, _ = _engine()
    engine.world.worktrees.append(worktree(str(repo), "feat2", is_main=False))
    engine.world.worktrees = [w for w in engine.world.worktrees if w.path != FEAT]
    engine.world.worktrees[-1].branch = "feat"
    engine.refresh_worktrees()
    _orchestrator(engine)
    _away(engine)
    _agent(engine, str(repo), "a1", "running")
    engine.head_sha = AsyncMock(return_value=head)
    methods = EngineMethods(engine)
    record = await _ask(engine)
    await methods.decision_answer(
        {"id": record["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", "after the answer")
    _agent(engine, str(repo), "a1", "waiting")
    result = (await methods.decision_revert({"id": record["id"]}))["decision"]
    assert result["reverted"]["results"][0].startswith(f"reset to {head[:7]}")

    second = await _ask(engine)
    await methods.decision_answer(
        {"id": second["id"], "choice": "A", "reason": "r", "caller": "tok-orch"}
    )
    _git(repo, "commit", "-q", "--allow-empty", "-m", "pushed")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "feat")
    result = (await methods.decision_revert({"id": second["id"]}))["decision"]
    assert result["reverted"]["results"] == ["reset skipped: commits since the answer were pushed"]


async def test_the_wake_waits_for_a_loop():
    """No event loop (a sync pass): the line waits for the next pass."""
    engine, _ = _engine()
    _orchestrator(engine)
    engine.first_responder.mailbox.post((ORCHESTRATOR_KEY, ""), "x")

    def no_loop(coro):
        coro.close()
        raise RuntimeError("no loop")

    engine._spawn = no_loop
    engine.first_responder.mailbox.flush()
    assert engine.first_responder.mailbox._letters
    await asyncio.sleep(0)
