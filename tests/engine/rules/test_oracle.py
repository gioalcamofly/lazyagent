"""The built-in catalog against the oracle (the old ``derive``, kept verbatim
in oracle.py): every golden derivation case, 10 000 seeded random Facts, and
every ``derive`` call the status, inbox and protocol suites make
(docs/design/rules-plan.md §4)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from lazyagent.engine import actionables
from lazyagent.engine.rules import builtin
from lazyagent.engine.rules.facts import RowContext
from lazyagent.engine.status import AgentFact, Facts, derive
from lazyagent.engine.trackers import TrackerFact
from lazyagent.models import CiCheck, PrInfo
from tests.engine.rules import oracle
from tests.engine.test_status import DERIVATION, NOW

ROOT = Path(__file__).resolve().parents[3]
SEED = 14_0014
SAMPLES = 10_000

STATUSES = [
    "running", "starting", "waiting", "completed", "interrupted", "waiting_for_user",
    "waiting_for_approval", "failed", "possibly_hanged",
]
CHECKS = {
    "none": [],
    "green": [CiCheck("tests", "COMPLETED", "SUCCESS")],
    "red": [CiCheck("lint", "COMPLETED", "SUCCESS"), CiCheck("tests", "COMPLETED", "FAILURE")],
    "red_first": [CiCheck("build", "COMPLETED", "FAILURE")],
    "running": [CiCheck("tests", "IN_PROGRESS", "")],
}
BALLS = ["you", "agent", "others", "stalled", "done"]


def _pick(r: random.Random, *choices, none: float = 0.0):
    if none and r.random() < none:
        return None
    return r.choice(choices)


def random_facts(r: random.Random) -> tuple[Facts, float]:
    """A Facts with every field drawn from what the engine produces (and a
    few odd values: an empty PR state, an unknown review), and a last
    change from now to two days back."""
    agents = tuple(
        AgentFact(f"a{i}", r.choice(STATUSES), r.choice(["", "Bash", "Edit a.py", "x " * 7]),
                  NOW - r.random() * 3600)
        for i in range(r.choice([0, 0, 1, 1, 1, 2, 3]))
    )
    pr = None
    if r.random() < 0.7:
        pr = PrInfo(
            r.randint(1, 999), "A title",
            r.choice(["OPEN"] * 6 + ["MERGED", "CLOSED", ""]),
            list(CHECKS[r.choice(list(CHECKS))]),
            "https://example.com/pull/1",
            review_decision=r.choice(["APPROVED", "APPROVED", "CHANGES_REQUESTED",
                                      "REVIEW_REQUIRED", "", "", "ODD"]),
            mergeable=r.choice(["MERGEABLE", "MERGEABLE", "CONFLICTING", "UNKNOWN", ""]),
            review_requests=_pick(r, 0, 0, 1, 2, none=0.3),
            reviews=_pick(r, 0, 0, 1, none=0.3),
        )
    queue = None
    if r.random() < 0.6:
        queue = tuple(
            {"state": r.choice(["approve: Bash", "CI red: tests", "changes requested"]),
             "level": r.choice(["now", "round", "deep"]),
             "blocks_agent": r.random() < 0.3,
             "kind": r.choice(["approval", "ci", "review"])}
            for _ in range(r.choice([0, 0, 0, 1, 2]))
        )
    gesture = None
    g = r.random()
    if g < 0.08:
        gesture = {"kind": "label", "ball": r.choice(BALLS), "text": "QA", "at": NOW}
    elif g < 0.16:
        gesture = {"kind": "park", "at": NOW,
                   "until": r.choice([NOW + 3600, NOW + 3 * 86400, NOW + 30 * 86400,
                                      "indefinite", "ci_done", "agent_stopped",
                                      "human_review", "change", "odd"])}
        if r.random() < 0.3:
            gesture["text"] = r.choice(["", "after the release", "a b c d e f g"])
        if r.random() < 0.3:
            gesture["waiting"] = True
            if r.random() < 0.5:
                gesture["source"] = "board"
    elif g < 0.2:
        gesture = {"kind": "close", "at": NOW}
        if r.random() < 0.5:
            gesture["how"] = r.choice(["dropped", "done"])
    tracker = None
    if r.random() < 0.25:
        tracker = TrackerFact(
            "jira", "K-1", "https://example.com/K-1", "Task", "In QA",
            _pick(r, *BALLS, none=0.3), r.choice(["in QA", "waiting for the release train"]),
            _pick(r, "now", "round", "deep", none=0.5), NOW, r.random() < 0.2,
        )
    report = None
    if r.random() < 0.2:
        report = {"at": NOW, "needs_user": r.random() < 0.4,
                  "ball": r.choice(["", "others", "stalled", "agent", "you"]),
                  "note": r.choice(["", "waiting for QA", "one two three four five six"])}
        if r.random() < 0.2:
            del report["note"]
    delegation = None
    if r.random() < 0.1:
        delegation = {"agent": "a1", "phase": r.choice(["on_it", "over"]), "at": NOW,
                      "fact": r.choice(["", "CI red: tests", "conflict with base"])}
    facts = Facts(
        key="repo:feat", repo="repo", branch=r.choice(["feat", None]),
        worktree_path="/r-feat", agents=agents, pr=pr,
        restorable=r.choice([0, 0, 0, 1, 2]),
        actionables=queue,
        claim={"at": NOW} if r.random() < 0.07 else None,
        tracker=tracker,
        gesture=gesture,
        report=report,
        holder=_pick(r, "orchestrator", "someone", none=0.9),
        hold={"at": NOW, "note": r.choice(["", "checking CI", None])} if r.random() < 0.07
        else None,
        delegation=delegation,
        merged_into=_pick(r, "develop", "main", none=0.85),
        activity_at=_pick(r, NOW - 3600, NOW - 30 * 3600, none=0.7),
    )
    return facts, NOW - r.choice([0, 600, 23 * 3600, 25 * 3600, 48 * 3600])


def both(facts: Facts, last_change: float, stalled_hours: float = 24):
    new = derive(facts, NOW, last_change, stalled_hours=stalled_hours)
    old = oracle.derive(facts, NOW, last_change, stalled_hours=stalled_hours)
    return new, old


@pytest.mark.parametrize("case", sorted(DERIVATION))
def test_every_golden_case_matches_the_oracle(case):
    f, _ = DERIVATION[case]
    for last_change in (NOW, NOW - 25 * 3600):
        new, old = both(f, last_change)
        assert new == old


def test_ten_thousand_seeded_random_facts_match_the_oracle():
    r = random.Random(SEED)
    catalog = builtin()
    hit: set[str] = set()
    mismatches = []
    for i in range(SAMPLES):
        facts, last_change = random_facts(r)
        hours = r.choice([24, 24, 1, 72])
        new, old = both(facts, last_change, hours)
        if new != old:
            mismatches.append((i, facts, new, old))
        ctx = RowContext(facts, NOW, last_change, hours)
        hit.add(catalog.rules[catalog.match(ctx)].id)
    assert not mismatches, mismatches[:3]
    # The generator reaches every rule but the inherited ones' own matches.
    assert hit == {rule.id for rule in catalog.rules}


def test_the_catalog_keeps_the_kinds_in_their_old_order_and_places():
    # The catalog's 14 come first; code may register more after them.
    assert [(k.name, k.level, k.title, k.rank, k.blocks_agent)
            for k in list(actionables.KINDS.values())[:14]] == [
        ("approval", "now", "Needs approval", 1, True),
        ("question", "now", "Question", 2, True),
        ("hanged", "round", "Hanged?", 3, False),
        ("failed", "round", "Failed", 4, False),
        ("exited", "round", "Exited", 4, False),
        ("ci", "round", "CI failing", 5, False),
        ("ci_failed", "round", "CI failed", 5, False),
        ("review", "deep", "Changes requested", 5, False),
        ("conflict", "round", "Merge conflict", 5, False),
        ("done", "deep", "Your turn", 6, False),
        ("job", "round", "Job failed", 7, False),
        ("tracker_status", "round", "Tracker", 5, False),
        ("report", "round", "Agent reports", 5, False),
        ("decision", "deep", "Decide", 2, False),
    ]


SUITES = [
    "tests/engine",
    "tests/protocol",
    "tests/contract",
    "tests/test_mcp_status.py",
    "tests/test_status_cli.py",
]


def test_every_derive_call_of_the_status_suites_matches_the_oracle(tmp_path, capsys):
    """The engine, protocol and contract suites, run once more with every
    ``status.derive`` call checked against the oracle (oracle_plugin.py):
    the golden fixtures' rows, the projection's passes, the gestures."""
    report = tmp_path / "oracle.json"
    env = {**os.environ, "LAZYAGENT_ORACLE_REPORT": str(report)}
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.engine.rules.oracle_plugin", "--ignore=tests/engine/rules",
         "-x", *SUITES],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500,
    )
    seen = json.loads(report.read_text()) if report.exists() else {}
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-3000:], seen)
    assert seen["mismatches"] == []
    with capsys.disabled():
        print(f"\nORACLE {seen['calls']} derive calls checked, no mismatch")
    # It did look: the suites derive thousands of rows.
    assert seen["calls"] > 1000
