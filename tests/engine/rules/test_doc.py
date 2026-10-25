""""How lazyagent works" (proposal 0014 R6): concepts.toml, the generated
sentences and templates, the page as Markdown, and the committed copies
(docs/guides/how-lazyagent-works.md, the desktop's rules_catalog.json) that
`lazyagent rules doc --write` regenerates.

HOME and every XDG dir are under the test's tmp_path (conftest.py)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lazyagent.engine.rules import builtin_text, doc, load
from lazyagent.engine.rules.export import catalog_payload

ROOT = Path(__file__).resolve().parents[3]
NO_FILES = {"files": [], "errors": [], "warnings": []}
REGENERATE = "run `uv run lazyagent rules doc --write` and commit the result"


def test_the_committed_page_is_what_rules_doc_generates():
    committed = (ROOT / doc.DOC_PATH).read_text(encoding="utf-8")
    assert committed == doc.page_markdown(doc.builtin_payload()), REGENERATE


def test_the_desktops_copy_of_the_catalog_is_current():
    committed = (ROOT / doc.COPY_PATH).read_text(encoding="utf-8")
    assert committed == doc.copy_json(), REGENERATE


def test_a_changed_state_text_changes_the_page_with_no_hand_edit():
    text = builtin_text()
    assert 'state = "merge: approved, CI green"' in text
    changed = load(text.replace('state = "merge: approved, CI green"', 'state = "merge: QA passed"'))
    page = doc.page_markdown(catalog_payload(changed, NO_FILES))
    assert "merge: QA passed" in page and "merge: approved, CI green" not in page
    # So the committed page no longer matches until `rules doc` runs again.
    assert page != (ROOT / doc.DOC_PATH).read_text(encoding="utf-8")


def test_every_screenshot_the_page_names_is_committed_and_linked():
    page = doc.page_markdown(doc.builtin_payload())
    shots = [s["screenshot"] for s in doc.concepts()["sections"] if s["screenshot"]]
    assert sorted(shots) == ["decision", "expanded", "home", "park", "strip"]
    for name in shots:
        assert (ROOT / doc.SCREENSHOTS / f"{name}.png").is_file(), name
        assert f"](../../{doc.SCREENSHOTS}/{name}.png)" in page
    # The links resolve from docs/guides/.
    assert (ROOT / "docs/guides" / f"../../{doc.SCREENSHOTS}/home.png").resolve().is_file()


def test_home_md_points_to_the_page():
    home = (ROOT / "docs/guides/home.md").read_text(encoding="utf-8")
    assert "how-lazyagent-works.md" in home


def test_concepts_are_checked():
    c = doc.concepts()
    assert [z["id"] for z in c["zones"]] == list(doc.ZONES)
    assert {s["generated"] for s in c["sections"]} - {None} == set(doc.GENERATED)
    assert {k["where"] for k in c["keys"]} == set(doc.KEY_PLACES)
    # The page's own key and the Why? key are there.
    assert any(k["keys"] == "F1" for k in c["keys"])
    assert any(k["keys"] == "w" and k["where"] == "home" for k in c["keys"])
    # Every built-in action's key reads as the desktop writes it.
    payload = doc.builtin_payload()
    assert payload["concepts"] == c
    assert all(not k["keys"].startswith("Shift+") for k in c["keys"])


@pytest.mark.parametrize(("when", "said"), [
    ('pr.state == "open" and ci.state in ["green", "none"]',
     "The PR's state is open and CI on the PR is green or none"),
    ("agents.running == 0", "There are no agents at work"),
    ("items.count > 0", "There are open inbox items on the work item"),
    ('pr.merged_into != ""', "There is a branch that already holds the head"),
    ('ci.first_failing == ""', "There is no first failing check"),
    ('report.note != ""', "The agent's report note is not empty"),
    ("not pr.exists", "There is no PR"),
    ("not claim.active", "You did not pass it to the orchestrator"),
    ("not place.is_orchestrator", "Not the orchestrator's own place"),
    ("not items.blocks_agent", "No open inbox item blocks an agent"),
    ("not gesture.waiting", "No feeder's Waiting lane"),
    ('not queue.enabled and "failed" in agents.statuses',
     "The inbox queue is off and the agents' statuses include failed"),
    ("item.idle > 10m", "The time since the last sign of work is over 10m"),
    ("known(tracker.level)", "The task's mapped level is known"),
    ("true", "Always"),
])
def test_sentences_read_the_facts_phrases(when, said):
    assert doc.sentence(when) == said


@pytest.mark.parametrize(("expr", "shown"), [
    ('with("CI red", ci.first_failing)', "CI red: ‹the first failing check›"),
    ('gesture.how == "dropped" ? "dropped by you" : "closed by you"', "dropped by you / closed by you"),
    ('(pr.review == "none" ? "PR open" : "review requested") + (ci.state == "green" ? ", CI green" : "")',
     "(PR open / review requested)(, CI green)"),
    ('clip("orchestrator · " + hold.note, 8)', "orchestrator · ‹the orchestrator's hold note›"),
])
def test_templates_show_the_holes(expr, shown):
    assert doc.template(expr) == shown


def test_every_rule_and_kind_is_annotated():
    payload = doc.builtin_payload()
    zones = {r["id"]: r["zone"] for r in payload["rules"]}
    assert zones["merge"] == "round" and zones["queue"] == "inbox" and zones["park"] == "parked"
    assert zones["closed.merged"] == "done" and zones["no_agent"] == "flight"
    assert zones["changes_requested"] == "deep"
    assert all(r["sentence"] and "zone" in r and "state_text" in r for r in payload["rules"])
    off = {r["id"] for r in payload["rules"] if r["queue_off"]}
    assert {"approve", "answer", "changes_requested"} <= off and "merge" not in off
    kinds = {k["id"]: k for k in payload["kinds"]}
    assert kinds["approval"]["zone"] == "now" and kinds["approval"]["state_text"].startswith("approve")


def test_the_page_lists_every_state_verb_kind_fact_and_action():
    payload = doc.builtin_payload()
    page = doc.page_markdown(payload)
    for r in payload["rules"]:
        assert f"`{r['id']}`" in page
        if r["state_text"]:
            assert r["state_text"].replace("|", "\\|") in page
    for k in payload["kinds"]:
        assert f"**{k['verb']}**" in page
    for f in payload["facts"]:
        assert f"`{f['name']}`" in page
    for a in payload["actions"]:
        assert f"`{a['id']}`" in page
    for k in payload["concepts"]["keys"]:
        assert f"`{k['keys']}`" in page
    assert "Shift+" not in page


def test_your_rules_show_in_the_effective_page(tmp_path):
    from tests.engine.rules.test_user_rules import catalog_of, rule

    catalog, errors, warnings = catalog_of(("global", rule("qa_gate", 'tracker.status == "QA"')))
    payload = catalog_payload(catalog, {"files": [], "errors": errors, "warnings": warnings})
    page = doc.page_markdown(payload)
    assert "`qa_gate` (global, /global/rules.toml:1)" in page
    assert "The task's status is QA" in page


def test_cli_rules_doc_prints_and_writes(tmp_path, capsys):
    from lazyagent.engine.cli import rules_main

    assert rules_main(["doc", str(ROOT)]) == 0
    assert capsys.readouterr().out == doc.page_markdown(doc.builtin_payload())
    # --write needs a checkout: a bare repo dir has no docs/guides.
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert rules_main(["doc", "--write", str(repo)]) == 1
    assert "is not there" in capsys.readouterr().err
    (repo / "docs/guides").mkdir(parents=True)
    (repo / "desktop/crates/app/assets").mkdir(parents=True)
    assert rules_main(["doc", "--write", str(repo)]) == 0
    assert (repo / doc.DOC_PATH).read_text() == doc.page_markdown(doc.builtin_payload())
    assert json.loads((repo / doc.COPY_PATH).read_text()) == json.loads(doc.copy_json())
    assert rules_main(["doc", "--write", "--effective", str(repo)]) == 2


def test_the_rules_example_on_the_page_is_a_valid_rules_file():
    from lazyagent.engine.rules import builtin
    from lazyagent.engine.rules.user import compose, parse_file

    example = next(s["example"] for s in doc.concepts()["sections"] if s["id"] == "rules")
    composed = compose(builtin(), [parse_file(example, "global", "/global/rules.toml")])
    assert composed.errors == [] and composed.warnings == []
    merge = composed.catalog.by_id["merge"]
    assert merge.origin == "global" and merge.and_when is not None


def test_why_names_the_rules_above_a_merge_items_rule_in_english():
    from tests.protocol.golden_cases import _call, _status_engine
    import asyncio

    result = asyncio.run(_call(_status_engine(), "rules_explain", {"key": "repo:docs-index"}))[2]["result"]
    item = result["item_rule"]
    assert item["id"] == "merge"
    above = {a["id"]: a for a in item["above"]}
    assert above["approve"] == {"id": "approve", "origin": "builtin", "failed": "not queue.enabled",
                                "said": "The inbox queue is off", "value": "false"}
    assert all(a["said"] for a in result["above"])
