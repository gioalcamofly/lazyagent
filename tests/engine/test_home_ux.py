"""Home UX fixes (proposal 0013, I4e): approvals and questions answered from
Home, ages from activity, undo, close and stop, who holds the ball, plain
states, and what the orchestrator is doing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.engine.actionables import UNDO_FOR
from lazyagent.engine.approvals import ESC, prompt_keys
from lazyagent.engine.events import AGENT_EXITED
from lazyagent.engine.manifest import Entry
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.notes import NotesStore
from lazyagent.engine.orchestrator import midnight
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"
RED = [CiCheck("tests", "COMPLETED", "FAILURE")]

CLAUDE = """\
╭──────────────────────────────────────────────╮
│ Bash command                                 │
│                                              │
│   npm test                                   │
│                                              │
│ Do you want to proceed?                      │
│ ❯ 1. Yes                                     │
│   2. Yes, and don't ask again for npm        │
│   3. No, and tell Claude what to do (esc)    │
╰──────────────────────────────────────────────╯
"""
CODEX = """\
  Allow command?

  $ cargo test

› 1. Yes, proceed (y)
  2. Yes, and don't ask again for this command (a)
  3. No, and tell Codex what to do differently (esc)
"""
NO_PROMPT = "> Do you want to try again? I can rerun the tests.\n"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine():
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
        config=Config(orchestrator=OrchestratorConfig()),
    )
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _agent(engine, path, agent_id, status, detail="", screen=""):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id, detail=detail
    )
    if agent_id not in engine.agent_ids(path):
        session = MagicMock()
        session.worktree_path, session.agent_id = path, agent_id
        session.running = True
        session.send_input = AsyncMock()
        session.catch_up = AsyncMock()
        add_session(engine, session)
    session = engine.get_session(path, agent_id)
    session.screen_text.return_value = screen
    return session


def _drop(engine, path, agent_id):
    engine.registry.states.get(path, {}).pop(agent_id, None)
    engine._agents.get(path, {}).pop(agent_id, None)


def _orchestrator(engine, status="waiting"):
    session = _agent(engine, ORCHESTRATOR_KEY, "", status)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    return session


def _worker_token(engine, agent_id="a1"):
    engine._tokens[f"tok-{agent_id}"] = (FEAT, agent_id)
    return f"tok-{agent_id}"


def _pr(checks=(), **kw):
    return PrInfo(7, "Feat", "OPEN", list(checks), "https://example.com/pull/7", **kw)


def _row(engine, key=KEY):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key)


def _texts(engine, kind):
    return [e["text"] for e in engine.overlay.entries(KEY) if e["kind"] == kind]


async def _settle(engine):
    engine.status.flush()
    await engine.wait_idle()


# --- 1. Tool approvals from Home ---------------------------------------------


async def test_prompt_keys_reads_claude_and_codex_menus_and_nothing_else():
    assert prompt_keys(CLAUDE) == {"allow": "1", "deny": ESC}
    assert prompt_keys(CODEX) == {"allow": "y", "deny": ESC}
    assert prompt_keys(NO_PROMPT) is None
    assert prompt_keys("") is None
    # The question without the menu under it (scrolled away) is not a prompt.
    assert prompt_keys("Do you want to proceed?\n\n> _\n") is None
    # A menu above the latest question does not count either.
    assert prompt_keys(CLAUDE + "\nDo you want to keep going?\n> _\n") is None


async def test_an_approval_is_answered_by_key_when_its_prompt_is_on_screen():
    engine, _ = _engine()
    session = _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash", CLAUDE)
    result = await EngineMethods(engine).approval_answer({"key": KEY, "allow": True})
    assert result == {"id": "Approval:/tmp/repo-feat/a1", "key": KEY, "agent_id": "a1",
                      "allow": True, "sent": "allow"}
    session.write.assert_called_once_with("1")
    session.send_input.assert_not_awaited()
    assert _texts(engine, "approval") == ["approved: Bash"]
    assert engine.overlay.entries(KEY)[-1]["actor"] == "user"


async def test_a_codex_approval_is_denied_by_id_with_esc():
    engine, _ = _engine()
    session = _agent(engine, FEAT, "a1", "waiting_for_approval", "cargo test", CODEX)
    result = await EngineMethods(engine).approval_answer(
        {"id": "Approval:/tmp/repo-feat/a1", "allow": False}
    )
    assert result["sent"] == "deny"
    session.write.assert_called_once_with(ESC)
    assert _texts(engine, "approval") == ["denied: cargo test"]


async def test_no_key_is_written_when_the_prompt_is_not_on_screen():
    engine, _ = _engine()
    session = _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash", NO_PROMPT)
    with pytest.raises(ValueError, match=r"^The permission prompt is not on agent a1's screen: "
                                         r"open its terminal \(o\)$"):
        await EngineMethods(engine).approval_answer({"key": KEY, "allow": True})
    session.write.assert_not_called()
    assert _texts(engine, "approval") == []


async def test_approvals_are_the_users_only():
    engine, _ = _engine()
    session = _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash", CLAUDE)
    _orchestrator(engine)
    methods = EngineMethods(engine)
    for token in ("tok-orch", _worker_token(engine)):
        with pytest.raises(ValueError, match="Tool approvals are the user's to answer"):
            await methods.approval_answer({"key": KEY, "allow": True, "caller": token})
    session.write.assert_not_called()


async def test_an_approval_needs_its_item_and_a_waiting_agent():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="No open approval on repo:feat"):
        await methods.approval_answer({"key": KEY, "allow": True})
    with pytest.raises(ValueError, match="allow must be a boolean"):
        await methods.approval_answer({"key": KEY, "allow": "yes"})
    _agent(engine, FEAT, "a1", "running", "Bash", CLAUDE)
    with pytest.raises(ValueError, match="No open approval"):
        await methods.approval_answer({"id": "Approval:/tmp/repo-feat/a1", "allow": True})


# --- 2. Questions from Home ---------------------------------------------------


async def test_a_question_is_answered_through_the_mailbox_when_the_agent_is_idle(monkeypatch):
    engine, _ = _engine()
    session = _agent(engine, FEAT, "a1", "waiting_for_user", "Which db?")
    mailbox = engine.first_responder.mailbox
    flush = mailbox.flush
    # Posted while the agent sits at its question, then it moves on before
    # the mailbox runs: nothing is typed into its turn.
    monkeypatch.setattr(mailbox, "flush", lambda: None)
    result = await EngineMethods(engine).question_answer(
        {"key": KEY, "text": "Postgres,\nwe already run it."}
    )
    assert result == {"key": KEY, "agent_id": "a1", "queued": True, "decision": None}
    assert mailbox.has(f"question:{FEAT}/a1")
    _agent(engine, FEAT, "a1", "running")
    monkeypatch.setattr(mailbox, "flush", flush)
    mailbox.flush()
    await engine.wait_idle()
    session.send_input.assert_not_awaited()
    _agent(engine, FEAT, "a1", "waiting")
    mailbox.flush()
    await engine.wait_idle()
    assert session.send_input.await_args.args[0] == "Postgres, we already run it."
    assert _texts(engine, "decision") == ["answered: Postgres, we already run it."]
    assert engine.notes is None or not engine.notes.list()


async def test_a_routed_question_record_is_answered_as_a_decision():
    engine, _ = _engine()
    _orchestrator(engine, "running")
    session = _agent(engine, FEAT, "a1", "waiting_for_user", "Which db?")
    engine.status.flush()
    [record] = engine.decisions.list()
    assert record["source"] == "question" and record["holder"] == "orchestrator"
    result = await EngineMethods(engine).question_answer({"key": KEY, "text": "Postgres"})
    await engine.wait_idle()
    assert (result["decision"]["id"], result["decision"]["state"]) == (record["id"], "answered")
    assert result["decision"]["answered_by"] == "user"
    assert session.send_input.await_args.args[0] == f"Decision {record['id']}: Postgres."


async def test_questions_are_the_users_and_need_an_open_one():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="No open question on repo:feat"):
        await methods.question_answer({"key": KEY, "text": "x"})
    session = _agent(engine, FEAT, "a1", "waiting_for_user", "Which db?")
    with pytest.raises(ValueError, match="text is required"):
        await methods.question_answer({"key": KEY, "text": "  "})
    _orchestrator(engine)
    for token in ("tok-orch", _worker_token(engine)):
        with pytest.raises(ValueError, match="Questions are the user's to answer"):
            await methods.question_answer({"key": KEY, "text": "x", "caller": token})
    session.send_input.assert_not_awaited()


# --- 3. Ages from activity ----------------------------------------------------


async def test_an_old_branch_seen_at_engine_start_is_aged_from_its_commit():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    row = _row(engine)
    assert (row["state"], row["changed_at"], row["activity_at"]) == (
        "no activity", NOW - 3 * DAY, NOW - 3 * DAY,
    )
    # PR info arrives after the start: the facts change, the age does not.
    clock.now += 60
    engine.worktree_service._pr_infos[FEAT] = _pr(review_decision="APPROVED", mergeable="UNKNOWN")
    row = _row(engine)
    assert (row["ball"], row["changed_at"]) == ("stalled", NOW - 3 * DAY)


async def test_notes_edits_count_as_activity(tmp_path):
    engine, _ = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    engine.open_manifest(tmp_path / "manifest.json")
    engine.notes = NotesStore(tmp_path / "notes", clock=lambda: NOW - 2 * HOUR)
    meta = engine.notes.create(FEAT, "a1", notes_id="0a1b2c3d4e51")
    entry = Entry(Entry.new_id(), FEAT, "a1", "agent", "claude", ended_at=NOW,
                  notes_id=meta.notes_id)
    engine.manifest.restorable[entry.id] = entry
    row = _row(engine)
    assert (row["state"], row["changed_at"], row["activity_at"]) == (
        "agent to restore", NOW - 2 * HOUR, NOW - 2 * HOUR,
    )


async def test_only_agent_changes_seen_after_the_start_are_activity():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    _agent(engine, FEAT, "a1", "waiting")
    # Found idle when the engine starts: not activity.
    assert _row(engine)["activity_at"] == NOW - 3 * DAY
    clock.now += 60
    _agent(engine, FEAT, "a1", "running")
    assert _row(engine)["activity_at"] == NOW + 60
    clock.now += 60
    _drop(engine, FEAT, "a1")
    row = _row(engine)
    assert (row["state"], row["changed_at"]) == ("no agent", NOW + 120)


async def test_a_users_step_is_activity_and_the_engines_is_not():
    engine, clock = _engine()
    engine.status._commit_times = {"feat": NOW - 3 * DAY}
    engine.overlay.add(KEY, "ci", "engine", "CI green", at=NOW - HOUR)
    assert _row(engine)["activity_at"] == NOW - 3 * DAY
    engine.overlay.add(KEY, "outward", "agent:a1", "pr_create", at=NOW - HOUR)
    row = _row(engine)
    assert (row["activity_at"], row["state"]) == (NOW - HOUR, "no agent")


# --- 4. Undo --------------------------------------------------------------------


async def test_undismiss_brings_back_an_exit_with_its_occurrence():
    engine, clock = _engine()
    engine.actionables.start()
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    _drop(engine, FEAT, "a1")
    engine.events.emit(AGENT_EXITED, {"worktree_path": FEAT, "agent_id": "a1",
                                      "session_id": "s", "exit_code": 3, "stopped": False})
    [item] = engine.actionables.list()["items"]
    methods = EngineMethods(engine)
    await methods.actionables_mark({"id": item["id"], "action": "dismiss"})
    assert engine.actionables.list()["items"] == []
    clock.now += 5
    back = (await methods.actionables_mark({"id": item["id"], "action": "undismiss"}))["item"]
    assert back["occurrence"] == item["occurrence"]
    assert [i["id"] for i in engine.actionables.list()["items"]] == [item["id"]]
    with pytest.raises(ValueError, match="Nothing to undo"):
        await methods.actionables_mark({"id": item["id"], "action": "undismiss"})
    # Past the undo window an exit cannot come back.
    await methods.actionables_mark({"id": item["id"], "action": "dismiss"})
    clock.now += UNDO_FOR
    with pytest.raises(ValueError, match="Nothing to undo"):
        await methods.actionables_mark({"id": item["id"], "action": "undismiss"})


async def test_undismiss_restores_a_returned_delegation():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _agent(engine, FEAT, "a1", "waiting")
    methods = EngineMethods(engine)
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    await _settle(engine)
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    _agent(engine, FEAT, "a1", "waiting")
    item = _row(engine)["needs_you"]["top"]
    assert engine.delegations.get(KEY)["phase"] == "returned"
    await methods.actionables_mark({"id": item, "action": "dismiss"})
    assert engine.delegations.get(KEY) is None
    await methods.actionables_mark({"id": item, "action": "undismiss"})
    assert engine.delegations.get(KEY)["phase"] == "returned"
    assert _row(engine)["state"] == "agent a1 finished; still CI red: tests"


async def test_undo_park_brings_back_the_label_it_replaced():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="not parked by you"):
        await methods.status_gesture({"key": KEY, "kind": "undo", "what": "park"})
    await methods.status_gesture({"key": KEY, "kind": "label", "ball": "others", "text": "QA"})
    await methods.status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    assert _row(engine)["zone"] == "parked"
    result = await methods.status_gesture({"key": KEY, "kind": "undo", "what": "park"})
    assert (result["undone"], result["gesture"]["kind"], result["gesture"]["text"]) == (
        "park", "label", "QA",
    )
    assert _texts(engine, "undo") == ["undone: park"]
    # Without a label before it, the park simply goes.
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    await methods.status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    assert (await methods.status_gesture({"key": KEY, "kind": "undo", "what": "park"}))[
        "gesture"] is None


async def test_undo_delegate_says_whether_the_line_reached_the_agent():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    _agent(engine, FEAT, "a1", "waiting_for_approval", "Bash")
    methods = EngineMethods(engine)
    # The agent is on a dialog: the line waits in the mailbox.
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent",
                                  "item": f"Ci:{FEAT}:7"})
    mailbox = engine.first_responder.mailbox
    assert mailbox.has(f"delegate:{KEY}")
    result = await methods.status_gesture({"key": KEY, "kind": "undo", "what": "delegate"})
    assert (result["delegation_sent"], result["row"]["delegation"]) == (False, None)
    assert not mailbox.has(f"delegate:{KEY}")
    # Idle: the line goes at once, and undo says so.
    _agent(engine, FEAT, "a1", "waiting")
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    await _settle(engine)
    result = await methods.status_gesture({"key": KEY, "kind": "undo", "what": "delegate"})
    assert result["delegation_sent"] is True
    with pytest.raises(ValueError, match="no delegation"):
        await methods.status_gesture({"key": KEY, "kind": "undo", "what": "delegate"})


async def test_undo_pass_takes_back_the_claim_but_not_a_hold():
    engine, _ = _engine()
    _orchestrator(engine)
    methods = EngineMethods(engine)
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    result = await methods.status_gesture({"key": KEY, "kind": "undo", "what": "pass"})
    assert (result["claim"], engine.claims.get(KEY)) == (None, None)
    await methods.orchestrator_hold({"key": KEY, "note": "verifying", "caller": "tok-orch"})
    with pytest.raises(ValueError, match="not passed to the orchestrator"):
        await methods.status_gesture({"key": KEY, "kind": "undo", "what": "pass"})
    assert engine.claims.get_hold(KEY) is not None
    with pytest.raises(ValueError, match="what must be one of"):
        await methods.status_gesture({"key": KEY, "kind": "undo", "what": "label"})


# --- 5. Close and stop ------------------------------------------------------------


async def test_close_and_stop_stops_the_agents_and_nothing_else():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    _agent(engine, FEAT, "a2", "waiting")
    engine.worktree_service._pr_infos[FEAT] = _pr()
    engine.refresh_pr_info = AsyncMock()
    stops = []
    engine.stop_agent = lambda path, agent_id, notes="delete": (
        stops.append((path, agent_id, notes)), _drop(engine, path, agent_id))
    methods = EngineMethods(engine)
    result = await methods.status_gesture({"key": KEY, "kind": "close", "how": "done",
                                           "stop_agents": True})
    assert result["stopped"] == ["a1", "a2"]
    assert stops == [(FEAT, "a1", "keep"), (FEAT, "a2", "keep")]
    assert (result["row"]["state"], result["row"]["agents"]) == ("closed by you", [])
    assert any(wt.path == FEAT for wt in engine.worktrees) and engine.world.removed == []
    assert engine.worktree(FEAT).branch == "feat"
    engine.refresh_pr_info.assert_not_awaited()


async def test_close_without_stop_agents_leaves_them_running():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "waiting")
    result = await EngineMethods(engine).status_gesture({"key": KEY, "kind": "close"})
    assert result["stopped"] == []
    assert engine.agent_ids(FEAT) == ["a1"]
    with pytest.raises(ValueError, match="stop_agents must be a boolean"):
        await EngineMethods(engine).status_gesture(
            {"key": KEY, "kind": "close", "stop_agents": "yes"})


# --- 6. Who holds the ball --------------------------------------------------------


async def test_the_holder_follows_the_rule():
    engine, _ = _engine()
    methods = EngineMethods(engine)
    assert _row(engine)["holder"] is None  # stalled
    _agent(engine, FEAT, "a1", "running")
    assert (_row(engine)["rule"], _row(engine)["holder"]) == (9, "agent")
    _agent(engine, FEAT, "a1", "waiting")
    engine.worktree_service._pr_infos[FEAT] = _pr(review_decision="APPROVED", mergeable="UNKNOWN")
    assert (_row(engine)["state"], _row(engine)["holder"]) == ("agent idle", "agent")
    _drop(engine, FEAT, "a1")
    engine.worktree_service._pr_infos[FEAT] = _pr(RED)
    assert (_row(engine)["zone"], _row(engine)["holder"]) == ("inbox", "user")
    _orchestrator(engine)
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    assert (_row(engine)["rule"], _row(engine)["holder"]) == (13, "orchestrator")
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    await methods.orchestrator_hold({"key": KEY, "note": "verifying", "caller": "tok-orch"})
    assert (_row(engine)["rule"], _row(engine)["holder"]) == (15, "orchestrator")
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    _agent(engine, FEAT, "a1", "waiting")
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    row = _row(engine)
    assert (row["rule"], row["holder"], row["state"]) == (
        16, "agent", "agent a1 · fixing CI red: tests")
    await methods.status_gesture({"key": KEY, "kind": "clear"})
    _drop(engine, FEAT, "a1")
    engine.worktree_service._pr_infos[FEAT] = _pr(review_requests=1, reviews=0)
    assert (_row(engine)["ball"], _row(engine)["holder"]) == ("others", None)
    engine.worktree_service._pr_infos[FEAT] = _pr([CiCheck("tests", "IN_PROGRESS", "")],
                                                  review_requests=1)
    # CI running with no agent: nobody works on it.
    assert (_row(engine)["state"], _row(engine)["holder"]) == ("CI running", None)
    await methods.status_gesture({"key": KEY, "kind": "park", "until": "indefinite"})
    assert (_row(engine)["zone"], _row(engine)["holder"]) == ("parked", None)


async def test_a_routed_question_is_the_orchestrators_and_a_report_the_agents():
    engine, _ = _engine()
    _orchestrator(engine, "running")
    _agent(engine, FEAT, "a1", "waiting_for_user", "Which db?")
    engine.status.flush()
    row = _row(engine)
    assert (row["rule"], row["holder"]) == (14, "orchestrator")
    _agent(engine, FEAT, "a1", "running")
    engine.status.flush()
    _agent(engine, FEAT, "a1", "waiting")
    await EngineMethods(engine).status_report(
        {"note": "on the db migration", "ball": "agent", "caller": _worker_token(engine)})
    engine.worktree_service._pr_infos[FEAT] = _pr(review_requests=1)
    row = _row(engine)
    assert (row["rule"], row["ball"], row["holder"]) == (12, "agent", "agent")


# --- 8. What the orchestrator is doing ----------------------------------------------


async def test_status_list_shows_the_orchestrators_report_and_queue():
    engine, clock = _engine()
    methods = EngineMethods(engine)
    assert engine.status.list()["orchestrator"] == {"alive": False, "report": None, "queue": []}
    _orchestrator(engine)
    engine.worktrees.append(worktree("/tmp/repo-fix", "fix", is_main=False))
    await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator",
                                  "ttl_s": 7200})
    await methods.orchestrator_hold({"key": "repo:fix", "note": "waiting on the merge",
                                     "ttl_s": 600, "caller": "tok-orch"})
    clock.now += 5
    await methods.status_report({"note": "verifying feat", "caller": "tok-orch"})
    orchestrator = engine.status.list()["orchestrator"]
    assert orchestrator["alive"] is True
    assert orchestrator["report"] == {"note": "verifying feat", "at": NOW + 5}
    assert [(q["key"], q["kind"], q["note"], q["until"], q["by"], q["auto"])
            for q in orchestrator["queue"]] == [
        ("repo:fix", "hold", "waiting on the merge", NOW + 600, "orchestrator", False),
        (KEY, "pass", "", NOW + 7200, "user", False),
    ]
    # Persisted with the overlay.
    assert engine.overlay._serialize().count("verifying feat") == 1


async def test_orchestrator_actions_lists_its_steps_newest_first():
    engine, clock = _engine()
    _orchestrator(engine)
    engine.first_responder.presence.connected = lambda: True
    engine.first_responder.presence.set("away")
    engine.head_sha = AsyncMock(return_value="abc123")
    _agent(engine, FEAT, "a1", "running")
    worker = _worker_token(engine)
    methods = EngineMethods(engine)
    # Yesterday's: not in today's list.
    engine.overlay.add(KEY, "action", "orchestrator", "re-ran CI", action="ci_rerun",
                       at=midnight(NOW) - 60)
    brief = {
        "question": "Fixed or exponential backoff?",
        "options": [{"key": "A", "label": "Exponential"}, {"key": "B", "label": "Fixed"}],
        "recommendation": "A", "tried": "fixed", "reversible": "yes",
        "evidence": [{"kind": "url", "url": "https://example.com/run/1"}],
        "impact_if_wrong": "slow", "caller": worker,
    }
    record = (await methods.request_decision(brief))["decision"]
    clock.now += 10
    await methods.decision_answer({"id": record["id"], "choice": "A", "reason": "flaky",
                                   "caller": "tok-orch"})
    clock.now += 10
    await methods.orchestrator_hold({"key": KEY, "note": "checking", "caller": "tok-orch"})
    actions = (await methods.orchestrator_actions({}))["actions"]
    assert [(a["kind"], a["action"], a["decision_id"], a["revertible"]) for a in actions] == [
        ("delegate", None, None, False),
        ("decision", "answer_reversible", record["id"], True),
    ]
    assert actions[1]["reason"] == "flaky" and actions[1]["mandate"] == "away"
    every = (await methods.orchestrator_actions({"since": 0, "caller": "tok-orch"}))["actions"]
    assert every[-1]["text"] == "re-ran CI"
    await methods.decision_revert({"id": record["id"]})
    actions = (await methods.orchestrator_actions({}))["actions"]
    assert [a["revertible"] for a in actions] == [False, False]
    with pytest.raises(ValueError, match="the user's or the orchestrator's"):
        await methods.orchestrator_actions({"caller": worker})


async def test_a_row_shows_its_last_step_but_not_ball_or_state_changes():
    engine, clock = _engine()
    assert _row(engine)["last_step"] is None
    engine.overlay.add(KEY, "outward", "agent:a1", "pr_create: opened")
    clock.now += 5
    engine.overlay.add(KEY, "ball", "engine", "stalled → agent: working")
    assert _row(engine)["last_step"] == {"at": NOW, "kind": "outward", "actor": "agent:a1",
                                         "text": "pr_create: opened"}
