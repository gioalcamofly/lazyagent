"""Opt-in auto-delegation of stalled items (proposal 0013 §7, Q-B33): the
fixed rule, its limits, the engine brief after two failures, and the
``[inbox]`` settings written to ``.lazyagent.toml``."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, InboxConfig, load_config, set_table_keys
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import AgentState, AgentStatus, PrInfo
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
KEY = "repo:feat"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(branch="feat", **inbox):
    inbox.setdefault("auto_delegate", ["conflict_with_base"])
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, branch, is_main=False)],
        config=Config(inbox=InboxConfig(**inbox)),
    )
    clock = Clock()
    engine.status.clock = clock
    engine.autodelegate.teammate = AsyncMock(return_value=False)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        7, "Feat", "OPEN", [], mergeable="CONFLICTING"
    )
    return engine, clock


def _agent(engine, agent_id, status, path=FEAT):
    engine.registry.states.setdefault(path, {})[agent_id] = AgentState(
        status=AgentStatus(status), agent_id=agent_id
    )
    if agent_id not in engine.agent_ids(path):
        session = MagicMock()
        session.worktree_path, session.agent_id = path, agent_id
        session.running = True
        session.send_input = AsyncMock()
        add_session(engine, session)
    return engine.get_session(path, agent_id)


async def _pass(engine):
    engine.status.flush()
    await engine.wait_idle()
    engine.status.flush()
    await engine.wait_idle()


def _sent(session):
    return [c.args[0] for c in session.send_input.await_args_list]


async def test_off_by_default_nothing_is_delegated():
    engine, _ = _engine(auto_delegate=[])
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    agent.send_input.assert_not_called()


async def test_two_attempts_to_the_owner_then_a_brief_to_sit_down_with():
    engine, _ = _engine()
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    [text] = _sent(agent)
    assert text.startswith("lazyagent auto-delegation (attempt 1 of 2): branch feat has a merge")
    assert any(e["kind"] == "delegate" for e in engine.overlay.entries(KEY))

    # The agent works on it (the item goes), stops, the conflict is still there.
    _agent(engine, "a1", "running")
    await _pass(engine)
    _agent(engine, "a1", "waiting")
    await _pass(engine)
    assert len(_sent(agent)) == 2 and "attempt 2 of 2" in _sent(agent)[1]

    _agent(engine, "a1", "running")
    await _pass(engine)
    engine.overlay.report(KEY, FEAT, "a1", note="rebased; app.py still conflicts", ball=None,
                          needs_user=False, label="Agent a1", now=engine.status.clock() + 1)
    _agent(engine, "a1", "waiting")
    await _pass(engine)
    assert len(_sent(agent)) == 2, "no third attempt"
    [brief] = engine.decisions.list()
    assert (brief["source"], brief["class"], brief["holder"]) == ("auto_delegate", "deep", "user")
    first, second = brief["brief"]["tried"].splitlines()[:2]
    # Each attempt says what the agent did: no commit, its last report.
    assert first.endswith("sent to agent a1 · no commit was made")
    assert second.endswith('sent to agent a1 · no commit was made · '
                           'its last report: "rebased; app.py still conflicts"')
    assert any(i["kind"] == "decision" and i["level"] == "deep"
               for i in engine.actionables.list()["items"])


async def test_a_cleared_condition_resets_the_attempts():
    engine, _ = _engine()
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    _agent(engine, "a1", "running")
    await _pass(engine)
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", [], mergeable="MERGEABLE")
    _agent(engine, "a1", "waiting")
    await _pass(engine)
    assert engine.autodelegate.attempts == {}
    engine.worktree_service._pr_infos[FEAT] = PrInfo(7, "Feat", "OPEN", [], mergeable="CONFLICTING")
    await _pass(engine)
    assert "attempt 1 of 2" in _sent(agent)[-1]


async def test_never_on_a_protected_branch():
    engine, _ = _engine(branch="master")
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    agent.send_input.assert_not_called()


async def test_never_on_a_teammates_branch():
    engine, _ = _engine()
    engine.autodelegate.teammate = AsyncMock(return_value=True)
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    await _pass(engine)
    agent.send_input.assert_not_called()
    assert engine.decisions.list() == []


async def test_the_daily_cap():
    engine, _ = _engine(auto_delegate_max_per_day=1)
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    _agent(engine, "a1", "running")
    await _pass(engine)
    _agent(engine, "a1", "waiting")
    await _pass(engine)
    assert len(_sent(agent)) == 1


async def test_with_no_owner_one_is_spawned_with_the_brief(monkeypatch):
    engine, _ = _engine(auto_delegate=["agent_dead"])
    engine.worktree_service._pr_infos.pop(FEAT)
    spawned = []
    modes = []

    async def spawn(path, instruction=None, **kw):
        spawned.append((path, instruction))
        modes.append(kw.get("permission_mode"))
        _agent(engine, "a2", "running")
        return engine.get_agent(path, "a2")

    monkeypatch.setattr(engine, "spawn_agent", spawn)
    engine.actionables.start()  # it hears agent.exited
    _agent(engine, "a1", "running")
    engine.status.flush()
    # a1 exits on its own.
    engine._forget_agent(FEAT, "a1", 1, stopped=False)
    await _pass(engine)
    [(path, text)] = spawned
    assert path == FEAT and "an agent that exited" in text
    assert modes == ["bypass"], "no agent on record: [inbox] auto_delegate_skip_permissions (bypass)"


async def test_to_the_orchestrator_it_is_a_claim():
    engine, _ = _engine(auto_delegate_target="orchestrator")
    _agent(engine, "", "waiting", path=ORCHESTRATOR_KEY)
    _agent(engine, "a1", "waiting")
    await _pass(engine)
    claim = engine.claims.get(KEY)
    assert claim is not None and claim["by"] == "engine"


async def test_ci_red_is_accepted_but_inactive():
    engine, _ = _engine(auto_delegate=["ci_red"])
    assert engine.config.inbox.auto_delegate == ["ci_red"]
    agent = _agent(engine, "a1", "waiting")
    await _pass(engine)
    agent.send_input.assert_not_called()


# --- [inbox] in .lazyagent.toml ---------------------------------------------------


TOML = """# Team settings
default_branch = "main"

[inbox]
# what may be delegated on its own
auto_delegate = [
  "ci_red",
]
deep_max = 7  # the zone's number

[orchestrator]
claim_ttl_s = 3600
"""


async def test_inbox_config_set_round_trips_and_keeps_the_rest(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text(TOML)
    engine = make_engine([worktree(str(tmp_path))], repo_root=str(tmp_path))
    engine.worktree_service.repo_root = str(tmp_path)
    engine._config_loader = load_config
    methods = EngineMethods(engine)
    assert await methods.inbox_config_get({}) == {
        "repo": tmp_path.name, "auto_delegate": ["ci_red"], "auto_delegate_target": "owner",
        "auto_delegate_skip_checks": [],
    }
    result = await methods.inbox_config_set(
        {"repo": tmp_path.name, "auto_delegate": ["conflict_with_base", "agent_dead"],
         "auto_delegate_target": "orchestrator"}
    )
    assert result == {
        "repo": tmp_path.name,
        "auto_delegate": ["conflict_with_base", "agent_dead"],
        "auto_delegate_target": "orchestrator",
        "auto_delegate_skip_checks": [],
    }
    assert "auto_delegate_skip_checks" not in (tmp_path / ".lazyagent.toml").read_text(), \
        "left out: not written"
    text = (tmp_path / ".lazyagent.toml").read_text()
    for kept in ("# Team settings", "# what may be delegated on its own",
                 "deep_max = 7  # the zone's number", "claim_ttl_s = 3600"):
        assert kept in text
    config = load_config(tmp_path)
    assert (config.inbox.deep_max, config.orchestrator.claim_ttl_s) == (7, 3600)
    assert engine.config.inbox.auto_delegate_target == "orchestrator", "reloaded"

    with pytest.raises(ValueError, match="auto_delegate must be a list of"):
        await methods.inbox_config_set({"auto_delegate": ["everything"]})
    with pytest.raises(ValueError, match="auto_delegate_target must be one of"):
        await methods.inbox_config_set({"auto_delegate_target": "anyone"})
    with pytest.raises(ValueError, match="not 'other'"):
        await methods.inbox_config_get({"repo": "other"})

    result = await methods.inbox_config_set(
        {"auto_delegate_skip_checks": ["eval-suite", " label-*", "eval-suite"]})
    assert result["auto_delegate_skip_checks"] == ["eval-suite", "label-*"]
    assert result["auto_delegate"] == ["conflict_with_base", "agent_dead"], "the rest kept"
    assert load_config(tmp_path).inbox.auto_delegate_skip_checks == ["eval-suite", "label-*"]
    with pytest.raises(ValueError, match="auto_delegate_skip_checks must be a list"):
        await methods.inbox_config_set({"auto_delegate_skip_checks": "eval-suite"})
    with pytest.raises(ValueError, match="auto_delegate_skip_checks must be a list"):
        await methods.inbox_config_set({"auto_delegate_skip_checks": [""]})


async def test_inbox_config_set_is_the_users_and_creates_the_file(tmp_path):
    engine = make_engine([worktree(str(tmp_path))], repo_root=str(tmp_path))
    engine.worktree_service.repo_root = str(tmp_path)
    _agent(engine, "", "running", path=ORCHESTRATOR_KEY)
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    methods = EngineMethods(engine)
    with pytest.raises(ValueError, match="the user's"):
        await methods.inbox_config_set({"auto_delegate": [], "caller": "tok-orch"})
    await methods.inbox_config_set({"auto_delegate": ["agent_dead"]})
    assert load_config(tmp_path).inbox.auto_delegate == ["agent_dead"]


async def test_a_table_defined_inline_is_left_to_the_user():
    with pytest.raises(ValueError, match="not a plain table"):
        set_table_keys('inbox = { deep_max = 3 }\n', "inbox", {"auto_delegate": []})
    added = set_table_keys('[inbox]\ndeep_max = 3\n\n[x]\ny = 1\n', "inbox",
                           {"auto_delegate": ["agent_dead"]})
    assert added == '[inbox]\ndeep_max = 3\nauto_delegate = ["agent_dead"]\n\n[x]\ny = 1\n'


async def test_a_failed_attempt_quotes_the_agents_notes():
    engine, _ = _engine()
    store = MagicMock()
    store.__contains__.return_value = True
    store.section_bullets.side_effect = lambda _id, name: {
        "Pending": ["- [ ] rebase", "- [ ] resolve app.py", "- [ ] push"],
        "Doubts": ["keep both imports?"],
    }[name]
    engine.notes = store
    engine.notes_id = lambda path, agent_id="": "n1"
    attempt = {"tries": ["10:00 sent to agent a1"], "agent": "a1",
               "sent": {"head": None, "at": NOW}}
    await engine.autodelegate._outcome(KEY, FEAT, attempt)
    assert attempt["tries"] == [
        "10:00 sent to agent a1 · its notes: Pending: - [ ] resolve app.py; Pending: - [ ] push; "
        "Doubts: keep both imports?"
    ]


# --- The permission mode of an agent it spawns ------------------------------------


def _manifest(*entries):
    from lazyagent.engine.manifest import Entry

    manifest = MagicMock()
    manifest.agents = {}
    manifest.restorable = {}
    for i, (path, bypass, at, kind) in enumerate(entries):
        e = Entry(id=f"e{i}", worktree_path=path, agent_id=f"a{i}", kind=kind, provider="claude",
                  skip_permissions=bypass, started_at=at)
        (manifest.agents if i % 2 else manifest.restorable)[(path, e.agent_id) if i % 2 else e.id] = e
    return manifest


async def test_a_spawned_agent_takes_the_mode_of_the_worktrees_last_agent():
    engine, _ = _engine()
    engine.manifest = _manifest((FEAT, False, 10.0, "agent"), (FEAT, True, 20.0, "agent"),
                                ("/tmp/other", False, 30.0, "agent"))
    assert engine.autodelegate.permissions(FEAT) == "bypass", "the newest of this worktree"
    engine.manifest = _manifest((FEAT, True, 10.0, "agent"), (FEAT, False, 20.0, "agent"))
    assert engine.autodelegate.permissions(FEAT) == "cli", "the newest ran in normal mode"
    engine.config.agent.permission_mode = "acceptEdits"
    assert engine.autodelegate.permissions(FEAT) == "acceptEdits", (
        "an older engine's normal mode: the configured one")
    engine.manifest.restorable["e0"].permission_mode = "plan"
    engine.manifest.agents[(FEAT, "a1")].permission_mode = "dontAsk"
    assert engine.autodelegate.permissions(FEAT) == "dontAsk", "the mode it recorded"


async def test_with_no_agent_on_record_the_inbox_key_decides_bypass_by_default(tmp_path):
    engine, _ = _engine()
    engine.manifest = _manifest(("/tmp/other", False, 10.0, "agent"))
    assert InboxConfig().auto_delegate_skip_permissions is True
    assert engine.autodelegate.permissions(FEAT) == "bypass"
    engine.manifest = None
    assert engine.autodelegate.permissions(FEAT) == "bypass"
    (tmp_path / ".lazyagent.toml").write_text("[inbox]\nauto_delegate_skip_permissions = false\n")
    engine.config.inbox = load_config(tmp_path).inbox
    assert engine.autodelegate.permissions(FEAT) == "cli", "normal mode when the user says so"
    engine.config.agent.permission_mode = "plan"
    assert engine.autodelegate.permissions(FEAT) == "plan", "normal: the configured mode"
    engine.config.agent.permission_mode = "bypass"
    assert engine.autodelegate.permissions(FEAT) == "cli", "never bypass when the user says so"
