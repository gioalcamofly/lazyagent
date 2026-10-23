"""Decision briefs (proposal 0013 §5b, I4): validation and the engine's
backstop, text caps, evidence inside the worktree, the per-agent limit, the
lifecycle, and the Quick / Deep classes in the inbox."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, InboxConfig
from lazyagent.engine import decisions as dec
from lazyagent.engine.decisions import DECISION_CREATED, DECISION_RESOLVED, check_brief
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"

OPTIONS = [
    {"key": "A", "label": "Exponential, cap 30 s", "consequence": "slower failure on outages"},
    {"key": "B", "label": "Fixed 2 s, 5 tries", "consequence": "3/30 runs time out"},
]
BRIEF = {
    "question": "Retries: fixed or exponential backoff?",
    "options": OPTIONS,
    "recommendation": "A",
    "tried": "fixed 2 s: 3 of 30 runs hit the 10 s limit",
    "evidence": [{"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"}],
    "reversible": "yes",
    "impact_if_wrong": "slower failures under load",
}


def _engine(config: Config | None = None):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)], config=config
    )
    engine.status.clock = lambda: NOW
    return engine


def _agent(engine, agent_id="a1", status="running", path=FEAT):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id
    )
    if agent_id not in engine.agent_ids(path):
        session = MagicMock()
        session.worktree_path, session.agent_id = path, agent_id
        session.running = True
        session.send_input = AsyncMock()
        add_session(engine, session)
        engine._tokens[f"tok-{agent_id}"] = (path, agent_id)


async def _ask(engine, agent="a1", **changes):
    return await EngineMethods(engine).request_decision(
        {**BRIEF, **changes, "caller": f"tok-{agent}"}
    )


def _row(engine):
    return next(r for r in engine.status.list(include_done=True)["rows"] if r["key"] == KEY)


def _items(engine):
    return engine.actionables.list()["items"]


# --- Validation ------------------------------------------------------------------


async def test_a_full_brief_is_valid_and_kept_as_given():
    brief, invalid, problems = check_brief(BRIEF, FEAT, "abc123")
    assert (invalid, problems) == ([], [])
    assert brief["options"] == OPTIONS
    assert brief["evidence"] == [
        {"kind": "diff", "path": "src/retry.py", "sha": "abc123", "hunk": "@@ -40,7 +40,12 @@"}
    ]


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"options": OPTIONS[:1]}, "fewer than 2 options"),
        ({"options": "A or B"}, "fewer than 2 options"),
        ({"recommendation": "C"}, "the recommendation is not one of the options"),
        ({"recommendation": None}, "the recommendation is not one of the options"),
        ({"evidence": []}, "no evidence"),
        ({"evidence": [{"kind": "diff", "path": "../other/x.py"}]}, "no evidence"),
        ({"evidence": [{"kind": "file", "path": "/etc/passwd"}]}, "no evidence"),
        ({"evidence": [{"kind": "url", "url": "javascript:alert(1)"}]}, "no evidence"),
        ({"evidence": [{"kind": "screenshot"}]}, "no evidence"),
    ],
)
async def test_what_makes_a_brief_invalid(change, reason):
    _, invalid, _ = check_brief({**BRIEF, **change}, FEAT, "abc123")
    assert reason in invalid


async def test_text_is_capped_and_plain():
    brief, invalid, problems = check_brief(
        {
            **BRIEF,
            "question": "why\n" + "q" * 300,
            "tried": "line one\n\n  line   two\n" + "t" * 700,
            "impact_if_wrong": "i" * 300,
            "options": [*OPTIONS, {"label": "x" * 200}, {"key": "D", "label": "d"},
                        {"key": "E", "label": "e"}],
        },
        FEAT,
        None,
    )
    assert "\n" not in brief["question"] and len(brief["question"]) == dec.QUESTION_CHARS
    assert brief["tried"].startswith("line one\nline two\n") and len(brief["tried"]) == 600
    assert len(brief["impact_if_wrong"]) == dec.IMPACT_CHARS
    assert [o["key"] for o in brief["options"]] == ["A", "B", "C", "D"]
    assert len(brief["options"][2]["label"]) == dec.LABEL_CHARS
    assert "only the first 4 options were kept" in problems
    assert invalid == []


async def test_evidence_paths_stay_inside_the_worktree_and_are_pinned():
    brief, _, problems = check_brief(
        {
            **BRIEF,
            "evidence": [
                {"kind": "file", "path": f"{FEAT}/docs/retry.md", "lines": "10-24"},
                {"kind": "diff", "path": "a/../b.py", "sha": "deadbeef"},
                {"kind": "diff", "path": "../repo-other/x.py"},
                {"kind": "log", "ref": "job:tests", "lines": "nope"},
                {"kind": "notes", "section": "Doubts"},
                {"kind": "url", "url": "https://example.com/x"},
            ]
            + [{"kind": "notes", "section": f"s{i}"} for i in range(5)],
        },
        FEAT,
        "abc123",
    )
    assert brief["evidence"][:5] == [
        {"kind": "file", "path": "docs/retry.md", "sha": "abc123", "lines": "10-24"},
        {"kind": "diff", "path": "b.py", "sha": "deadbeef"},
        {"kind": "log", "ref": "job:tests"},
        {"kind": "notes", "section": "Doubts"},
        {"kind": "url", "url": "https://example.com/x"},
    ]
    assert len(brief["evidence"]) == dec.MAX_EVIDENCE
    assert "diff evidence dropped: its path is not inside the worktree" in problems


async def test_reversible_must_be_known_and_a_question_is_required():
    brief, invalid, problems = check_brief({**BRIEF, "reversible": "maybe"}, FEAT, None)
    assert brief["reversible"] is None and invalid == []
    assert problems == ["reversible must be one of yes, partly, no"]
    with pytest.raises(ValueError, match="question is required"):
        check_brief({**BRIEF, "question": "  "}, FEAT, None)


# --- The inbox: Quick / Deep -------------------------------------------------------------


async def test_a_valid_brief_waits_in_to_sit_down_with():
    engine = _engine()
    _agent(engine)
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.event), {DECISION_CREATED})
    result = await _ask(engine)
    decision = result["decision"]
    assert (decision["id"], decision["state"], decision["class"], decision["valid"]) == (
        "d1", "open", "deep", True,
    )
    assert (decision["holder"], decision["replaces"], decision["sha"]) == ("user", None, "abc123")
    assert "To sit down with" in result["message"]
    assert seen == [DECISION_CREATED]

    [item] = _items(engine)
    assert (item["kind"], item["level"], item["class"], item["decision_id"]) == (
        "decision", "deep", "deep", "d1",
    )
    assert item["label"] == BRIEF["question"]
    assert item["detail"] == "brief by agent a1 · 2 options · yes"
    assert item["blocks_agent"] is False
    row = _row(engine)
    assert (row["zone"], row["level"], row["class"]) == ("inbox", "deep", "deep")
    counts = engine.status.list()["counts"]
    assert (counts["deep"], counts["deep_max"]) == (1, 5)


async def test_an_invalid_brief_is_a_quick_question_with_the_terminal():
    engine = _engine()
    _agent(engine)
    result = await _ask(engine, evidence=[])
    assert (result["decision"]["valid"], result["decision"]["class"]) == (False, "quick")
    assert "not a full brief (no evidence)" in result["message"]
    [item] = _items(engine)
    assert (item["level"], item["class"], item["label"], item["detail"]) == (
        "round", "quick", f"agent a1 asks: {BRIEF['question']}", "open terminal",
    )


async def test_blocking_sorts_first_and_a_quick_one_is_now():
    engine = _engine()
    _agent(engine)
    _agent(engine, "a2")
    await _ask(engine)
    await _ask(engine, "a2", **{"class": "quick", "blocking": True})
    first, second = _items(engine)
    assert (first["decision_id"], first["level"], first["blocks_agent"]) == ("d2", "now", True)
    assert (second["decision_id"], second["level"]) == ("d1", "deep")
    assert _row(engine)["blocks_agent"] is True


async def test_a_brief_stands_for_its_agents_question():
    engine = _engine()
    _agent(engine, status="waiting_for_user")
    assert [i["kind"] for i in _items(engine)] == ["question"]
    await _ask(engine, blocking=True)
    assert [i["kind"] for i in _items(engine)] == ["decision"]


async def test_deep_max_is_shown_never_enforced():
    engine = _engine(Config(inbox=InboxConfig(deep_max=1, max_briefs_per_agent=0)))
    _agent(engine)
    for _ in range(3):
        await _ask(engine)
    result = engine.actionables.list()
    assert (result["counts"]["deep"], result["counts"]["deep_max"]) == (3, 1)


# --- Limits and lifecycle --------------------------------------------------------------


async def test_a_third_open_brief_is_refused_with_a_message():
    engine = _engine()
    _agent(engine)
    await _ask(engine)
    await _ask(engine)
    with pytest.raises(ValueError, match=r"already have 2 open briefs \(d1, d2\)"):
        await _ask(engine)
    # Another agent has its own two.
    _agent(engine, "a2")
    assert (await _ask(engine, "a2"))["decision"]["id"] == "d3"
    # Replacing one is not a third.
    result = await _ask(engine, replaces="d1", question="Retries: which cap?")
    assert (result["decision"]["id"], result["decision"]["replaces"]) == ("d4", "d1")
    old = engine.decisions.get("d1")
    assert (old["state"], old["superseded_by"]) == ("superseded", "d4")
    with pytest.raises(ValueError, match="replaces must be one of your open briefs"):
        await _ask(engine, replaces="d3")


async def test_the_agent_withdraws_its_own_brief():
    engine = _engine()
    _agent(engine)
    _agent(engine, "a2")
    await _ask(engine)
    seen = []
    engine.events.subscribe(lambda e: seen.append(e.data["decision"]["state"]), {DECISION_RESOLVED})
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="No brief 'd1' of yours"):
        await methods.decision_withdraw({"id": "d1", "caller": "tok-a2"})
    result = await methods.decision_withdraw({"id": "d1", "caller": "tok-a1"})
    assert (result["decision"]["state"], result["decision"]["closed_at"]) == ("withdrawn", NOW)
    assert seen == ["withdrawn"] and _items(engine) == []
    with pytest.raises(ValueError, match="is withdrawn: nothing to withdraw"):
        await methods.decision_withdraw({"id": "d1", "caller": "tok-a1"})


async def test_open_briefs_expire_when_the_item_closes():
    engine = _engine()
    _agent(engine)
    await _ask(engine)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(9, "x", "MERGED", [])
    _row(engine)
    record = engine.decisions.get("d1")
    assert record["state"] == "expired"
    assert [e["text"] for e in engine.overlay.entries(KEY)][-1].startswith("expired: merged")
    assert _items(engine) == []


async def test_only_a_worker_asks():
    engine = _engine()
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="needs the agent's token"):
        await methods.request_decision(dict(BRIEF))
    _agent(engine, "", path=ORCHESTRATOR_KEY)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    with pytest.raises(ValueError, match="not the orchestrator"):
        await methods.request_decision({**BRIEF, "caller": "tok-orch"})


async def test_get_and_list_and_the_timeline():
    engine = _engine()
    _agent(engine)
    methods = EngineMethods(engine)
    await _ask(engine)
    await _ask(engine, evidence=[])
    await methods.decision_withdraw({"id": "d1", "caller": "tok-a1"})
    assert (await methods.decision_get({"id": "d2"}))["decision"]["valid"] is False
    with pytest.raises(ValueError, match="No decision"):
        await methods.decision_get({"id": "d9"})
    assert [d["id"] for d in (await methods.decisions_list({}))["decisions"]] == ["d2", "d1"]
    listed = await methods.decisions_list({"key": KEY, "state": "open"})
    assert [d["id"] for d in listed["decisions"]] == ["d2"]
    with pytest.raises(ValueError, match="state must be one of"):
        await methods.decisions_list({"state": "lost"})
    entries = [e for e in engine.overlay.entries(KEY) if e["kind"] == "decision"]
    assert [(e["actor"], e["text"].split(":")[0], e["ref"]) for e in entries] == [
        ("agent:a1", "asked", "d1"), ("agent:a1", "asked", "d2"), ("agent:a1", "withdrawn", "d1"),
    ]


async def test_briefs_survive_a_restart(tmp_path):
    from lazyagent.engine.overlay import overlay_path

    engine = _engine()
    engine.overlay.open(overlay_path("/tmp/repo"))
    _agent(engine)
    await _ask(engine)
    engine.overlay.save()

    again = _engine()
    again.overlay.open(overlay_path("/tmp/repo"))
    _agent(again)
    assert again.decisions.get("d1")["state"] == "open"
    assert [i["decision_id"] for i in _items(again)] == ["d1"]
    assert (await _ask(again))["decision"]["id"] == "d2"
