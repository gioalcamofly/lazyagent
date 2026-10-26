"""I4h: an agent's finished turn that ends with a question to the user leads
its row (kind ``turn_question``), above the user's rules and the built-in
PR rules; and Delegate / Pass only where an agent or the orchestrator can
act (the catalog's ``delegable`` / ``passable``)."""

from __future__ import annotations

import json

import pytest

from lazyagent.agent_observers import ClaudeHooksObserver
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.questions import last_assistant_text, turn_question
from lazyagent.engine.rules import catalog as cat
from lazyagent.engine.rules.catalog import CatalogError, load
from lazyagent.models import AgentStatus, PrInfo
from tests.engine.test_home_polish import (
    FEAT, GREEN, KEY, RED, _agent, _engine, _orchestrator, _pass, _row,
)

QUESTION = "Should I fix it in this PR, or open a separate task?"
MESSAGE = f"The flaky test is in the parser.\n\n{QUESTION} I haven't changed anything yet."

# The owner's own review again after new commits: a user rule, global.
OWNER_REVIEW_AGAIN = """version = 1

[[rule]]
id = "owner_review_again"
when = '''
pr.mine and not pr.draft and pr.commits_after_ready > 0 and agents.running == 0
'''
[rule.then]
ball = "you"
level = "round"
state = "new commits since your approval"
verb = "Review"
action = "open_pr"
"""


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    return tmp_path


def _rules(tmp_path, text: str = OWNER_REVIEW_AGAIN) -> None:
    path = tmp_path / "xdg_config_home" / "lazyagent" / "rules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _mine(**kw) -> PrInfo:
    return PrInfo(7, "Feat", "OPEN", list(GREEN), "https://example.com/pull/7",
                  author="me", is_draft=False, commits_after_ready=1, **kw)


def _asks(engine, question=QUESTION, status="waiting", agent="a1"):
    session = _agent(engine, FEAT, agent, status)
    engine.registry.states[FEAT][agent].question = question
    return session


def _items(engine):
    return [(i["kind"], i["label"]) for i in engine.actionables.list()["items"]]


def _fields(engine):
    return engine.status.fields_of(KEY)


# --- Detection -------------------------------------------------------------


@pytest.mark.parametrize("text, question", [
    (MESSAGE, QUESTION),
    ("Done.\n\nWhich one do you want?", "Which one do you want?"),
    ("Listo.\n\n¿Lo pongo en este PR o en otra tarea?", "¿Lo pongo en este PR o en otra tarea?"),
    ("Tests pass. **Merge it now?**", "Merge it now?"),
    ("Two ways to go. Which one?\n\n1. Keep the cache\n2. Drop it", "Which one?"),
    ("Two ways:\n\n- A: keep it\n- B: drop it\n\nWhich do you prefer (A or B)?",
     "Which do you prefer (A or B)?"),
])
def test_a_question_that_ends_the_message_is_found(text, question):
    assert turn_question(text) == question


@pytest.mark.parametrize("text", [
    "",
    "Done: the tests pass and the PR is up.",
    "Here is the fix:\n\n```python\nif ready?\n    go()\n```",
    "Here is the fix:\n\n```\nwhat?\n",
    "| case | result |\n|---|---|\n| empty? | ok |",
    "> Should we?\n> said the reviewer",
    "## Open questions?",
    "Run `make check?`",
    "    indented code?",
    # A question earlier, then a long ending that is not about it.
    "Was it the cache? It was not: the parser reads the header twice when the file "
    "ends without a newline, so the second read fails and the test retries forever.",
    "What broke? The parser. Why? It reads the header twice. Fixed in this branch.",
])
def test_no_question_in_code_tables_quotes_or_mid_message(text):
    assert turn_question(text) is None


def test_the_transcripts_last_assistant_message_is_read(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Old?"}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Done."}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": QUESTION}]}},
        {"type": "assistant", "isSidechain": True,
         "message": {"id": "s", "content": [{"type": "text", "text": "sub"}]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert last_assistant_text(path) == f"Done.\n\n{QUESTION}"
    assert last_assistant_text(tmp_path / "missing.jsonl") is None


def test_the_claude_hooks_carry_the_question_until_the_next_prompt(tmp_path):
    log = tmp_path / "hooks.jsonl"
    observer = ClaudeHooksObserver(str(log))

    def hook(**data):
        with log.open("a") as f:
            f.write(json.dumps(data) + "\n")
        return observer.poll()

    [stop] = hook(hook_event_name="Stop", session_id="s", last_assistant_message=MESSAGE)
    assert (stop.status, stop.question) == (AgentStatus.WAITING, QUESTION)
    [idle] = hook(hook_event_name="Notification", notification_type="idle_prompt")
    assert idle.question == QUESTION
    [prompt] = hook(hook_event_name="UserPromptSubmit", session_id="s", prompt="in this PR")
    assert (prompt.status, prompt.question) == (AgentStatus.RUNNING, "")
    # No last_assistant_message: the transcript's end.
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {
        "id": "m", "content": [{"type": "text", "text": "Ship it?"}]}}) + "\n")
    [stop] = hook(hook_event_name="Stop", session_id="s", transcript_path=str(transcript))
    assert stop.question == "Ship it?"
    [stop] = hook(hook_event_name="Stop", session_id="s", last_assistant_message="Done.")
    assert stop.question == ""


# --- The item leads the row ------------------------------------------------


@pytest.mark.asyncio
async def test_a_question_leads_the_row_over_the_users_owner_review_rule(_home):
    _rules(_home)
    engine, _ = _engine()
    engine.rules.reload()
    engine.review_requests.viewer = "me"
    engine.worktree_service._pr_infos[FEAT] = _mine()
    _agent(engine, FEAT, "a1", "waiting")
    await _pass(engine)
    # The real case: no question seen, the user's rule decides the row.
    assert _fields(engine)["verb"] == "Review"
    assert _row(engine)["state"] == "new commits since your approval"
    assert _fields(engine)["delegable"] is False and _fields(engine)["passable"] is False

    _asks(engine)
    await _pass(engine)
    row = _row(engine)
    assert (row["ball"], row["level"], row["needs_you"]["top"]) == (
        "you", "round", f"Turn_question:{FEAT}/a1")
    assert row["state"] == "answer: Should I fix it…"
    fields = _fields(engine)
    assert (fields["rule_id"], fields["verb"], fields["action"]) == ("queue", "Answer", "open_terminal")
    item = engine.actionables.list()["items"][0]
    assert (item["kind"], item["label"], item["title"]) == ("turn_question", QUESTION, "Asks you")
    assert item["detail"] == "Agent a1 asks · answer in its terminal"
    assert (item["delegable"], item["passable"]) == (False, False)
    # The user's rule item is still found, below the question.
    assert [k for k, _ in _items(engine)] == ["turn_question", "rule"]


@pytest.mark.asyncio
async def test_it_leads_over_the_built_in_merge_too():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        7, "Feat", "OPEN", list(GREEN), "https://example.com/pull/7",
        review_decision="APPROVED", mergeable="MERGEABLE")
    _asks(engine)
    await _pass(engine)
    assert [k for k, _ in _items(engine)][0] == "turn_question"
    assert _fields(engine)["verb"] == "Answer"


@pytest.mark.asyncio
async def test_it_clears_on_input_or_a_new_turn():
    engine, _ = _engine()
    _asks(engine)
    await _pass(engine)
    assert _items(engine) == [("turn_question", QUESTION)]
    # The user answers: a new turn starts (the registry drops the question).
    engine.registry.update_status(FEAT, "a1", AgentStatus.RUNNING, engine.registry.states[FEAT]["a1"].confidence)
    await _pass(engine)
    assert _items(engine) == []
    assert engine.registry.states[FEAT]["a1"].question == ""
    # The turn ends with no question: "Your turn" (no PR), not "Asks you".
    engine.registry.update_status(FEAT, "a1", AgentStatus.WAITING,
                                  engine.registry.states[FEAT]["a1"].confidence)
    await _pass(engine)
    assert [k for k, _ in _items(engine)] == ["done"]


@pytest.mark.asyncio
async def test_a_question_without_a_pr_is_asks_you_not_your_turn():
    engine, _ = _engine()
    _asks(engine, status="completed")
    await _pass(engine)
    assert [k for k, _ in _items(engine)] == ["turn_question"]
    assert _row(engine)["level"] == "round"


@pytest.mark.asyncio
async def test_never_the_orchestrators_own_question():
    from lazyagent.engine.registry import ORCHESTRATOR_KEY

    engine, _ = _engine()
    _orchestrator(engine)
    engine.registry.states[ORCHESTRATOR_KEY][""].question = "Shall I merge?"
    await _pass(engine)
    assert "turn_question" not in [k for k, _ in _items(engine)]


@pytest.mark.asyncio
async def test_a_brief_stands_for_the_question_its_turn_ends_with():
    engine, _ = _engine()
    engine._tokens["tok-a1"] = (FEAT, "a1")
    _asks(engine, question="See brief d1?")
    await EngineMethods(engine).request_decision({
        "caller": "tok-a1", "question": "Fixed or exponential backoff?",
        "options": [{"key": "A", "label": "Exponential"}, {"key": "B", "label": "Fixed"}],
        "recommendation": "A", "tried": "fixed: 3/30 time out",
        "evidence": [{"kind": "url", "url": "https://example.com/run/1"}],
    })
    await _pass(engine)
    assert [k for k, _ in _items(engine)] == ["decision"]


@pytest.mark.asyncio
async def test_a_held_orchestrator_workers_question_still_reaches_the_user():
    engine, _ = _engine()
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", 0)
    # Its turn ended with no question: the orchestrator holds it to verify.
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True
    assert _row(engine)["zone"] == "in_flight"
    # The orchestrator answers it; its next turn ends with a question.
    engine.registry.update_status(FEAT, "a1", AgentStatus.RUNNING,
                                  engine.registry.states[FEAT]["a1"].confidence)
    await _pass(engine)
    _asks(engine, status="completed")
    await _pass(engine)
    row = _row(engine)
    assert row["hold"] is not None, "the hold stays; the question shows through it"
    assert (row["ball"], row["level"], row["needs_you"]["top"]) == (
        "you", "round", f"Turn_question:{FEAT}/a1")
    # And a pass leaves it with the user, as an approval.
    engine.claims.holds.pop(KEY)
    _agent(engine, FEAT, "a2", "waiting")
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", list(RED),
                                                      "https://example.com/pull/7")
    await _pass(engine)
    await EngineMethods(engine).status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    row = _row(engine)
    assert row["needs_you"]["top"] == f"Turn_question:{FEAT}/a1"
    assert orch is not None


# --- Delegate and pass -----------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_is_refused_on_a_review_rule_item_and_on_a_question(_home):
    _rules(_home)
    engine, _ = _engine()
    engine.rules.reload()
    engine.review_requests.viewer = "me"
    engine.worktree_service._pr_infos[FEAT] = _mine()
    _agent(engine, FEAT, "a1", "waiting")
    await _pass(engine)
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="Review on feat cannot be delegated: it is yours to do"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})
    _orchestrator(engine)
    with pytest.raises(ValueError, match="Review on feat is yours: the orchestrator cannot act"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "orchestrator"})
    _asks(engine)
    await _pass(engine)
    with pytest.raises(ValueError, match="Asks you on feat cannot be delegated: it is answered"):
        await methods.status_gesture({"key": KEY, "kind": "delegate", "to": "agent"})


@pytest.mark.asyncio
async def test_delegate_is_allowed_on_ci_red():
    engine, _ = _engine()
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", list(RED),
                                                      "https://example.com/pull/7")
    _agent(engine, FEAT, "a1", "waiting")
    await _pass(engine)
    item = engine.actionables.list()["items"][0]
    assert (item["kind"], item["delegable"], item["passable"]) == ("ci", True, True)
    assert _fields(engine)["delegable"] is True
    result = await EngineMethods(engine).status_gesture(
        {"key": KEY, "kind": "delegate", "to": "agent"})
    assert result["delegation"]["phase"] == "on_it"


def test_the_capabilities_default_by_verb_and_a_rule_may_say():
    kinds = {k.id: k for k in cat.load(open(cat.__file__.replace("catalog.py", "builtin.toml")).read()).kinds}
    delegable = {k for k, v in kinds.items() if v.can_delegate}
    assert delegable == {"hanged", "failed", "exited", "ci", "ci_failed", "review", "conflict",
                         "job", "tracker_status", "sync_base"}
    kept = {k for k, v in kinds.items() if not v.can_pass}
    assert kept == {"approval", "question", "turn_question", "decision", "review_request",
                    "attestation_confirm"}
    rules = load("""
[[rule]]
id = "qa"
num = 3
when = 'true'
[rule.then]
ball = "you"
state = "QA"
verb = "Review"
item = true
delegable = true
""").rules
    assert (rules[0].can_delegate, rules[0].can_pass) == (True, False)
    with pytest.raises(CatalogError, match="delegable must be true or false"):
        load("""
[[rule]]
id = "qa"
num = 3
when = 'true'
[rule.then]
ball = "you"
state = "QA"
delegable = "yes"
""")
