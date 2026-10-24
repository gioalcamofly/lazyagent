"""The decision view's engine side (proposal 0013 §5b, I5): the user's
answer delivered like a review reply and logged in the agent's notes, kept
on the work item and carried across a restore; Explain this and Request a
new brief through owner → explicit restore → orchestrator → none; the
evidence read."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from lazyagent.engine.explain import DECISION_EXPLAIN, MAX_LINES, cap_lines
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.overlay import KEEP_DECISIONS
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from tests.engine.test_notes_lifecycle import _notes_hook
from tests.engine.test_orchestrator_first_responder import (
    BRIEF,
    FEAT,
    FIX,
    KEY,
    _agent,
    _ask,
    _engine,
    _orchestrator,
    _sent,
)
from tests.engine.test_restore import World

pytestmark = pytest.mark.asyncio

QUESTION = BRIEF["question"]
PROMPT = (
    f'Explain brief d1 ("{QUESTION}") for the user in at most 10 lines; '
    'reply with decision_explain {id: "d1", text}.'
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


async def _settle(engine):
    engine.status.flush()
    await engine.wait_idle()


def _gone(engine, path, agent_id):
    """The agent's process is gone (stopped, crashed, the engine restarted)."""
    engine._agents[path].pop(agent_id)
    engine.registry.states.get(path, {}).pop(agent_id, None)


# --- The user's answer -------------------------------------------------------


async def test_the_users_answer_waits_until_the_agents_turn_is_over(tmp_path):
    engine, _ = _engine(tmp_path)
    worker = _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine, **{"class": "deep"})

    answered = (await EngineMethods(engine).decision_answer(
        {"id": record["id"], "choice": "b", "comment": "keep it simple"}
    ))["decision"]
    assert (answered["state"], answered["answered_by"], answered["revertible"]) == (
        "answered", "user", False,
    )
    # Mid-turn: nothing is typed into the agent's terminal.
    await _settle(engine)
    await _settle(engine)
    worker.send_input.assert_not_called()

    _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    assert _sent(worker) == ["Decision d1: B — Fixed 2 s, 5 tries. keep it simple"]
    await _settle(engine)
    assert len(_sent(worker)) == 1, "once"


async def test_the_answer_is_appended_to_the_agents_notes_decisions(tmp_path):
    engine, _ = _engine(tmp_path)
    _agent(engine, FEAT, "a1", "running")
    notes_id = engine.notes_id(FEAT, "a1")
    engine.notes.put(notes_id, "## Goal\nretries\n\n## Decisions\n- use sqlite\n", 0, "agent")
    record = await _ask(engine)
    await EngineMethods(engine).decision_answer(
        {"id": record["id"], "choice": "A", "comment": "matches the docs"}
    )
    notes = engine.notes.get(notes_id)
    assert notes.meta.by == "user"
    decisions = notes.text.split("## Decisions\n", 1)[1].splitlines()
    assert decisions[0] == "- use sqlite", "append only: what was there stays"
    assert decisions[1].startswith("- 20")
    assert decisions[1].endswith(f" · {QUESTION} → A (user): matches the docs")


async def test_an_answered_brief_stays_on_its_work_item_for_30_days(tmp_path):
    engine, clock = _engine(tmp_path)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    await EngineMethods(engine).decision_answer({"id": record["id"], "choice": "A"})
    # The work item's tombstone ran out: the copy stays.
    engine.overlay.forget(KEY)
    listed = await EngineMethods(engine).decisions_list({"key": KEY})
    assert [(d["id"], d["answer"]["choice"]) for d in listed["decisions"]] == [("d1", "A")]
    engine.overlay.prune(clock.now + KEEP_DECISIONS - 60)
    assert "d1" in engine.overlay.decisions
    engine.overlay.prune(clock.now + KEEP_DECISIONS + 60)
    assert "d1" not in engine.overlay.decisions


async def test_an_answer_to_a_gone_agent_reaches_it_once_it_is_back(tmp_path):
    engine, _ = _engine(tmp_path)
    _agent(engine, FEAT, "a1", "running")
    record = await _ask(engine)
    _gone(engine, FEAT, "a1")
    await EngineMethods(engine).decision_answer({"id": record["id"], "choice": "A"})
    await _settle(engine)
    assert engine.decisions.get("d1")["answer"]["delivery"] == "agent gone"

    # Restored (same agent id), idle once it has started.
    worker = _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    assert _sent(worker) == ["Decision d1: A — Exponential, cap 30 s."]
    answer = engine.decisions.get("d1")["answer"]
    assert "delivery" not in answer and answer["delivered_at"]


# --- Across a real restore (the acceptance) ----------------------------------


@pytest_asyncio.fixture
async def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        await w.stop_all()


def _token(engine, path, agent_id):
    return next(t for t, slot in engine._tokens.items() if slot == (path, agent_id))


async def test_a_restored_agent_sees_the_answer_after_compaction(world, tmp_path):
    overlay = tmp_path / "overlay.json"
    first = world.engine()
    first.overlay.open(overlay)
    feat = str(world.feat)
    agent = await first.spawn_agent(feat, "Build the importer")
    first.head_sha = AsyncMock(return_value=None)
    brief = await EngineMethods(first).request_decision(
        {**BRIEF, "caller": _token(first, feat, agent.agent_id)}
    )
    decision_id = brief["decision"]["id"]
    notes_id = first.notes_id(feat, agent.agent_id)
    first.notes.append(notes_id, "Decisions", "use sqlite", by="agent")
    first.overlay.save()
    await first.stop()

    # The next engine: the agent is restorable, the brief still open.
    second = world.engine()
    second.overlay.open(overlay)
    assert second.decisions.get(decision_id)["state"] == "open"
    await EngineMethods(second).decision_answer(
        {"id": decision_id, "choice": "B", "comment": "simpler"}
    )
    await _settle(second)
    assert second.decisions.get(decision_id)["answer"]["delivery"] == "agent gone"
    assert f"{QUESTION} → B (user): simpler" in second.notes.get(notes_id).text

    [restored] = (await second.restore_agents())["restored"]
    assert (restored["agent_id"], second.notes_id(feat, agent.agent_id)) == (
        agent.agent_id, notes_id,
    )
    await second.notes.flush()
    out = json.loads(
        _notes_hook(
            second, feat, agent.agent_id,
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": "c"},
        )
    )["hookSpecificOutput"]["additionalContext"]
    assert f"{QUESTION} → B (user): simpler" in out
    # And the line itself is on its way, sent once the agent is idle.
    second.first_responder.check(second.status.clock())
    record = second.decisions.get(decision_id)
    assert record["answer"].get("delivery") == "after restore" or record["answer"].get(
        "delivered_at"
    )


# --- Explain this: the chain -------------------------------------------------


def _explain(engine, **params):
    return EngineMethods(engine).decision_explain_request(params)


async def test_explain_goes_to_the_live_owner_when_its_turn_is_over():
    engine, _ = _engine()
    _orchestrator(engine)
    worker = _agent(engine, FEAT, "a1", "running")
    await _ask(engine, reversible="no")
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.data["explain"]), {DECISION_EXPLAIN})

    req = (await _explain(engine, id="d1", question="why not both?"))["explain"]
    assert (req["route"], req["to"], req["state"]) == ("owner", "agent:a1", "asked")
    await _settle(engine)
    worker.send_input.assert_not_called()
    _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    assert _sent(worker) == [PROMPT + " Their question: why not both?"]
    assert engine.decisions.get("d1")["explain"]["sent_at"]
    orchestrator = engine.get_session(ORCHESTRATOR_KEY, "")
    orchestrator.send_input.assert_not_called()

    reply = await EngineMethods(engine).decision_explain(
        {"id": "d1", "text": "A backs off on outages.\nB keeps runs short.", "caller": "tok-a1"}
    )
    assert reply["explain"]["by"] == "agent:a1"
    assert reply["explain"]["text"] == "A backs off on outages.\nB keeps runs short."
    assert "message" not in reply
    assert engine.decisions.get("d1")["explain"]["state"] == "answered"
    assert [r["state"] for r in seen] == ["asked", "asked", "answered"]  # asked, sent, answered


async def test_a_restorable_owner_is_restored_only_when_the_user_says_so():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine, reversible="no")
    _gone(engine, FEAT, "a1")
    entry = SimpleNamespace(id="r1", worktree_path=FEAT, agent_id="a1")
    engine.explainer._restorable = lambda path, agent: entry if path == FEAT else None

    async def restore(ids):
        assert ids == ["r1"]
        _agent(engine, FEAT, "a1", "running")
        return {"restored": [{"id": "r1", "agent_id": "a1"}], "failed": []}

    engine.restore_agents = AsyncMock(side_effect=restore)
    with pytest.raises(ValueError, match="restore: true"):
        await _explain(engine, id="d1")
    engine.restore_agents.assert_not_awaited()
    await _settle(engine)
    engine.get_session(ORCHESTRATOR_KEY, "").send_input.assert_not_called()

    req = (await _explain(engine, id="d1", restore=True))["explain"]
    assert (req["route"], req["to"]) == ("restore", "agent:a1")
    engine.restore_agents.assert_awaited_once()
    worker = engine.get_session(FEAT, "a1")
    await _settle(engine)
    worker.send_input.assert_not_called()  # still busy starting
    _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    assert _sent(worker) == [PROMPT]


async def test_a_failed_restore_is_reported():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine)
    _gone(engine, FEAT, "a1")
    entry = SimpleNamespace(id="r1", worktree_path=FEAT, agent_id="a1")
    engine.explainer._restorable = lambda path, agent: entry
    engine.restore_agents = AsyncMock(
        return_value={"restored": [], "failed": [{"id": "r1", "reason": "its worktree is gone"}]}
    )
    with pytest.raises(ValueError, match="Could not restore agent a1: its worktree is gone"):
        await _explain(engine, id="d1", restore=True)


async def test_with_no_owner_the_orchestrator_explains_with_pointers():
    engine, _ = _engine()
    orchestrator = _orchestrator(engine, "running")
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine, reversible="no")
    _gone(engine, FEAT, "a1")

    req = (await _explain(engine, id="d1"))["explain"]
    assert (req["route"], req["to"]) == ("orchestrator", "orchestrator")
    await _settle(engine)
    orchestrator.send_input.assert_not_called()
    _orchestrator(engine, "waiting")
    await _settle(engine)
    [text] = _sent(orchestrator)
    assert text.startswith(PROMPT)
    assert f"agent a1 is not running: read the item's notes (notes_read for {FEAT})" in text

    reply = await EngineMethods(engine).decision_explain(
        {"id": "d1", "text": "It is about retries.", "caller": "tok-orch"}
    )
    assert reply["explain"]["by"] == "orchestrator"


async def test_with_nobody_to_ask_explain_is_refused():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine)
    _gone(engine, FEAT, "a1")
    with pytest.raises(ValueError, match="Nobody can explain it"):
        await _explain(engine, id="d1")
    assert engine.explainer.list() == []


async def test_explain_works_on_any_inbox_item_by_its_key():
    engine, _ = _engine()
    worker = _agent(engine, FEAT, "a1", "waiting_for_approval", detail="Bash")
    [item] = engine.actionables.list()["items"]
    assert item["work_item"] == KEY and item["kind"] == "approval"
    req = (await _explain(engine, key=KEY))["explain"]
    assert (req["id"], req["key"], req["agent"], req["route"]) == (None, KEY, "a1", "owner")
    _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    [text] = _sent(worker)
    assert text.startswith(f"Explain item {KEY} (")
    assert text.endswith(f'reply with decision_explain {{key: "{KEY}", text}}.')
    reply = await EngineMethods(engine).decision_explain(
        {"key": KEY, "text": "It wants to run the tests.", "caller": "tok-a1"}
    )
    assert reply["explain"]["state"] == "answered"
    with pytest.raises(ValueError, match="No work item"):
        await _explain(engine, key="repo:nope")


# --- Token checks and the cap ------------------------------------------------


async def test_only_the_owner_or_the_orchestrator_explains_and_only_when_asked():
    engine, _ = _engine()
    _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    _agent(engine, FIX, "a2", "running")
    await _ask(engine, reversible="no")
    methods = EngineMethods(engine)
    text = {"id": "d1", "text": "because"}
    with pytest.raises(ValueError, match="Nobody asked"):
        await methods.decision_explain({**text, "caller": "tok-a1"})
    with pytest.raises(ValueError, match="the user's"):
        await methods.decision_explain_request({"id": "d1", "caller": "tok-a1"})
    await _explain(engine, id="d1")
    with pytest.raises(ValueError, match="needs your token"):
        await methods.decision_explain(text)
    with pytest.raises(ValueError, match="Only the item's own agent"):
        await methods.decision_explain({**text, "caller": "tok-a2"})
    with pytest.raises(ValueError, match="Unknown caller token"):
        await methods.decision_explain({**text, "caller": "tok-nope"})
    with pytest.raises(ValueError, match="text is required"):
        await methods.decision_explain({"id": "d1", "text": " ", "caller": "tok-a1"})
    await methods.decision_explain({**text, "caller": "tok-orch"})
    with pytest.raises(ValueError, match="Nobody asked"):
        await methods.decision_explain({**text, "caller": "tok-a1"})


async def test_an_explanation_is_capped_at_ten_lines():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine)
    await _explain(engine, id="d1")
    long = "\n".join(f"line {i}" for i in range(1, 15))
    reply = await EngineMethods(engine).decision_explain(
        {"id": "d1", "text": long, "caller": "tok-a1"}
    )
    assert reply["explain"]["text"].splitlines() == [f"line {i}" for i in range(1, 11)]
    assert reply["explain"]["truncated"] is True
    assert "first 10 lines" in reply["message"]
    assert cap_lines("a\n\n\nb") == ("a\nb", False)
    assert len(cap_lines("x" * 1000)[0]) <= 300
    assert MAX_LINES == 10


# --- Request a new brief -----------------------------------------------------


async def test_a_new_brief_from_the_owner_supersedes_the_old_one():
    engine, _ = _engine()
    _orchestrator(engine)
    worker = _agent(engine, FEAT, "a1", "running")
    await _ask(engine, reversible="no")
    req = (await EngineMethods(engine).decision_rebrief_request(
        {"id": "d1", "note": "add the cost of each"}
    ))["explain"]
    assert (req["kind"], req["route"]) == ("rebrief", "owner")
    _agent(engine, FEAT, "a1", "waiting")
    await _settle(engine)
    assert _sent(worker) == [
        f'The user asks for a new brief instead of brief d1 ("{QUESTION}"): add the cost '
        'of each. Send a new request_decision with replaces: "d1" (the old one closes as '
        "superseded)."
    ]
    # Reversible and the orchestrator is alive: still the user's (they asked).
    new = await _ask(engine, replaces="d1", reversible="yes")
    assert (new["id"], new["holder"], new["replaces"]) == ("d2", "user", "d1")
    old = engine.decisions.get("d1")
    assert (old["state"], old["superseded_by"]) == ("superseded", "d2")
    assert (old["rebrief"]["state"], old["rebrief"]["replaced_by"]) == ("answered", "d2")


async def test_with_no_owner_the_orchestrator_writes_the_new_brief():
    engine, _ = _engine()
    orchestrator = _orchestrator(engine)
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine, reversible="no")
    _gone(engine, FEAT, "a1")
    methods = EngineMethods(engine)
    # Not asked: the orchestrator writes no briefs.
    with pytest.raises(ValueError, match="not the orchestrator"):
        await methods.request_decision({**BRIEF, "replaces": "d1", "caller": "tok-orch"})
    await methods.decision_rebrief_request({"id": "d1"})
    await _settle(engine)
    [text] = _sent(orchestrator)
    assert "Write the brief with your token" in text
    new = (await methods.request_decision(
        {**BRIEF, "replaces": "d1", "caller": "tok-orch"}
    ))["decision"]
    assert (new["agent"], new["key"], new["holder"]) == ("a1", KEY, "user")
    assert engine.decisions.get("d1")["state"] == "superseded"
    [entry] = [e for e in engine.overlay.entries(KEY) if e["text"].startswith("asked: ")
               and e.get("ref") == new["id"]]
    assert entry["actor"] == "orchestrator"


async def test_a_new_brief_is_only_for_an_open_brief():
    engine, _ = _engine()
    _agent(engine, FEAT, "a1", "running")
    await _ask(engine)
    await EngineMethods(engine).decision_answer({"id": "d1", "choice": "A"})
    with pytest.raises(ValueError, match="d1 is answered"):
        await EngineMethods(engine).decision_rebrief_request({"id": "d1"})
    with pytest.raises(ValueError, match="id is required"):
        await EngineMethods(engine).decision_rebrief_request({"key": KEY})


# --- Evidence ----------------------------------------------------------------


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


async def test_the_evidence_read_gives_lines_at_the_briefs_commit(tmp_path):
    repo = tmp_path / "feat"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "docs").mkdir()
    (repo / "docs" / "retry.md").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    (repo / "run.log").write_text("".join(f"log {i}\n" for i in range(1, 21)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "docs")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                         text=True, check=True).stdout.strip()
    (repo / "docs" / "retry.md").write_text("changed\n")

    engine, _ = _engine(tmp_path)
    engine.worktree_service.worktrees[1].path = str(repo)
    engine.status.flush()
    _agent(engine, str(repo), "a1", "running")
    engine.notes.append(engine.notes_id(str(repo), "a1"), "Doubts", "is 30 s enough?", "agent")
    engine.head_sha = AsyncMock(return_value=sha)
    await _ask(engine, evidence=[
        {"kind": "file", "path": "docs/retry.md", "lines": "10-12"},
        {"kind": "log", "ref": "run.log", "lines": "5"},
        {"kind": "notes", "section": "doubts"},
        {"kind": "url", "url": "https://example.com/run/1"},
        {"kind": "log", "ref": "job:tests"},
    ])
    methods = EngineMethods(engine)

    async def read(index):
        return await methods.decision_evidence({"id": "d1", "index": index})

    file = await read(0)
    assert (file["start"], file["total"]) == (7, 40)
    assert file["text"].splitlines() == [f"line {i}" for i in range(7, 16)], "at its commit"
    log = await read(1)
    assert (log["start"], log["text"].splitlines()[0]) == (2, "log 2")
    notes = await read(2)
    assert notes["text"] == "- is 30 s enough?"
    assert (await read(3))["url"] == "https://example.com/run/1"
    # A CI job is read from the failed log (engine/actions.py): off with no repo.
    assert "CI tracking is off" in (await read(4))["error"]
    with pytest.raises(ValueError, match="index must be"):
        await read(9)
