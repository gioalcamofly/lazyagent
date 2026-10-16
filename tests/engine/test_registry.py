"""AgentRegistry: states, events and persisted agent ids."""

from __future__ import annotations

import json
import logging
import multiprocessing

import pytest

from lazyagent.agent_events import EXITED
from lazyagent.engine.registry import (
    ORCHESTRATOR_KEY,
    AgentIdStore,
    AgentRegistry,
    default_id_store_path,
)
from lazyagent.models import AgentState, AgentStatus, LifecycleConfidence

HIGH = LifecycleConfidence.HIGH


def _events(registry: AgentRegistry) -> list[tuple[str, str, str, str]]:
    events, _ = registry.events.since(0)
    return [(e.worktree_path, e.agent_id, e.previous_status, e.status) for e in events]


# --- States and events ---


def test_get_creates_once():
    registry = AgentRegistry()
    state = registry.get("/wt", "a1")
    assert state == AgentState(agent_id="a1")
    assert registry.get("/wt", "a1") is state
    assert registry.agents("/wt") == {"a1": state}
    assert registry.agents("/other") == {}


def test_status_change_publishes_exactly_one_event():
    registry = AgentRegistry()
    state = registry.update_status("/wt", "a1", AgentStatus.RUNNING, HIGH, "using Bash")
    assert (state.status, state.confidence, state.detail) == (
        AgentStatus.RUNNING, HIGH, "using Bash",
    )
    assert _events(registry) == [("/wt", "a1", "no_agent", "running")]


def test_same_status_publishes_nothing_but_updates_detail():
    registry = AgentRegistry()
    registry.update_status("/wt", "a1", AgentStatus.RUNNING, HIGH, "one")
    state = registry.update_status(
        "/wt", "a1", AgentStatus.RUNNING, LifecycleConfidence.LOW, "two"
    )
    assert len(_events(registry)) == 1
    assert (state.confidence, state.detail) == (LifecycleConfidence.LOW, "two")


def test_previous_status_follows_the_transitions():
    registry = AgentRegistry()
    for status in (AgentStatus.RUNNING, AgentStatus.WAITING, AgentStatus.RUNNING):
        registry.update_status("/wt", "a1", status, HIGH, status.value)
    events, _ = registry.events.since(0)
    assert [(e.previous_status, e.status, e.detail) for e in events] == [
        ("no_agent", "running", "running"),
        ("running", "waiting", "waiting"),
        ("waiting", "running", "running"),
    ]


def test_rollup_across_agents():
    registry = AgentRegistry()
    assert registry.rollup("/wt") == AgentStatus.NO_AGENT
    registry.update_status("/wt", "a1", AgentStatus.RUNNING, HIGH)
    registry.update_status("/wt", "a2", AgentStatus.COMPLETED, HIGH)
    assert registry.rollup("/wt") == AgentStatus.RUNNING
    registry.update_status("/wt", "a3", AgentStatus.WAITING_FOR_APPROVAL, HIGH)
    assert registry.rollup("/wt") == AgentStatus.WAITING_FOR_APPROVAL
    assert registry.rollup("/other") == AgentStatus.NO_AGENT


def test_drop_publishes_exited_once_and_prunes_the_bucket():
    registry = AgentRegistry()
    registry.register("/wt", "a1", label="Agent 1")
    registry.update_status("/wt", "a1", AgentStatus.WAITING, HIGH)
    registry.drop("/wt", "a1")
    registry.drop("/wt", "a1")
    registry.drop("/never", "a1")
    events, _ = registry.events.since(0)
    assert [(e.previous_status, e.status, e.label) for e in events] == [
        ("no_agent", "waiting", "Agent 1"),
        ("waiting", EXITED, "Agent 1"),
    ]
    assert "/wt" not in registry.states


def test_drop_keeps_the_other_agents():
    registry = AgentRegistry()
    registry.get("/wt", "a1")
    registry.get("/wt", "a2")
    registry.drop("/wt", "a1")
    assert list(registry.agents("/wt")) == ["a2"]


def test_drop_worktree_exits_every_agent_of_that_worktree_only():
    registry = AgentRegistry()
    registry.update_status("/wt", "a1", AgentStatus.RUNNING, HIGH)
    registry.update_status("/wt", "a2", AgentStatus.WAITING, HIGH)
    registry.update_status("/other", "a1", AgentStatus.RUNNING, HIGH)
    cursor = registry.events.cursor
    registry.drop_worktree("/wt")
    events, _ = registry.events.since(cursor)
    assert [(e.worktree_path, e.agent_id, e.previous_status, e.status) for e in events] == [
        ("/wt", "a1", "running", EXITED),
        ("/wt", "a2", "waiting", EXITED),
    ]
    assert list(registry.states) == ["/other"]


def test_orchestrator_uses_agent_id_empty():
    registry = AgentRegistry()
    registry.register(ORCHESTRATOR_KEY, "", session_name="repo-orchestrator")
    state = registry.update_status(ORCHESTRATOR_KEY, "", AgentStatus.RUNNING, HIGH)
    assert registry.get(ORCHESTRATOR_KEY) is state
    assert (state.agent_id, state.label, state.session_name) == ("", "", "repo-orchestrator")
    registry.drop(ORCHESTRATOR_KEY, "")
    assert _events(registry) == [
        (ORCHESTRATOR_KEY, "", "no_agent", "running"),
        (ORCHESTRATOR_KEY, "", "running", EXITED),
    ]


def test_registered_identity_fills_the_state():
    registry = AgentRegistry()
    registry.register("/wt", "a1", label="Reviewer", session_name="wt-a1")
    state = registry.update_status("/wt", "a1", AgentStatus.RUNNING, HIGH)
    assert (state.label, state.session_name) == ("Reviewer", "wt-a1")
    events, _ = registry.events.since(0)
    assert events[0].label == "Reviewer"


def test_register_after_the_state_exists_fills_it_too():
    registry = AgentRegistry()
    state = registry.get("/wt", "a1")
    registry.register("/wt", "a1", label="Agent 1", session_name="wt-a1")
    assert (state.label, state.session_name) == ("Agent 1", "wt-a1")


def test_unregister_forgets_the_identity():
    registry = AgentRegistry()
    registry.register("/wt", "a1", label="Agent 1", session_name="wt-a1")
    registry.unregister("/wt", "a1")
    registry.unregister("/wt", "a1")
    assert (registry.label("/wt", "a1"), registry.session_name("/wt", "a1")) == ("", "")
    assert registry.get("/wt", "a1").label == ""


# --- Agent ids ---


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "state" / "agent_ids.json"


def _ids(registry: AgentRegistry, key: str, n: int) -> list[str]:
    return [registry.new_agent_id(key) for _ in range(n)]


def test_default_store_lives_under_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_id_store_path() == tmp_path / "lazyagent" / "agent_ids.json"


@pytest.mark.parametrize("persisted", [False, True])
def test_ids_are_monotonic_within_a_run(persisted, store_path):
    store = AgentIdStore(store_path) if persisted else None
    assert _ids(AgentRegistry(id_store=store), "/wt", 3) == ["a1", "a2", "a3"]


@pytest.mark.parametrize("persisted", [False, True])
def test_ids_are_never_reused_after_a_drop(persisted, store_path):
    registry = AgentRegistry(id_store=AgentIdStore(store_path) if persisted else None)
    first = registry.new_agent_id("/wt")
    registry.register("/wt", first)
    registry.get("/wt", first)
    registry.drop("/wt", first)
    registry.unregister("/wt", first)
    assert registry.new_agent_id("/wt") == "a2"


def test_ids_survive_a_new_registry_on_the_same_file(store_path):
    _ids(AgentRegistry(id_store=AgentIdStore(store_path)), "/wt", 2)
    restarted = AgentRegistry(id_store=AgentIdStore(store_path))
    assert restarted.new_agent_id("/wt") == "a3"


def test_the_file_is_keyed_by_absolute_path(store_path, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    AgentRegistry(id_store=AgentIdStore(store_path)).new_agent_id("wt")
    data = json.loads(store_path.read_text())
    assert data == {"version": 1, "counters": {str(tmp_path / "wt"): 1}}


def test_worktrees_have_independent_counters(store_path):
    registry = AgentRegistry(id_store=AgentIdStore(store_path))
    assert registry.new_agent_id("/wt-a") == "a1"
    assert registry.new_agent_id("/wt-a") == "a2"
    assert registry.new_agent_id("/wt-b") == "a1"
    assert registry.new_agent_id("/wt-a") == "a3"


def test_missing_file_starts_at_one(store_path):
    assert not store_path.exists()
    assert AgentRegistry(id_store=AgentIdStore(store_path)).new_agent_id("/wt") == "a1"
    assert store_path.exists()


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"counters": [1]}', '{"version": 1}', ""],
)
def test_corrupt_file_falls_back_to_one_with_a_warning(content, store_path, caplog):
    store_path.parent.mkdir(parents=True)
    store_path.write_text(content)
    with caplog.at_level(logging.WARNING, logger="lazyagent.engine.registry"):
        registry = AgentRegistry(id_store=AgentIdStore(store_path))
        assert registry.new_agent_id("/wt") == "a1"
    assert "corrupt" in caplog.text
    # The next allocation reads the file it just rewrote.
    assert AgentRegistry(id_store=AgentIdStore(store_path)).new_agent_id("/wt") == "a2"


def test_corrupt_file_is_backed_up_before_it_is_replaced(store_path, caplog):
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="lazyagent.engine.registry"):
        AgentRegistry(id_store=AgentIdStore(store_path)).new_agent_id("/wt")
    backups = list(store_path.parent.glob("agent_ids.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{not json"
    assert str(backups[0]) in caplog.text
    assert json.loads(store_path.read_text())["counters"] == {"/wt": 1}


def test_only_the_newest_corrupt_backups_are_kept(store_path):
    store_path.parent.mkdir(parents=True)
    for i in range(5):
        store_path.write_text(f"corrupt {i}")
        AgentRegistry(id_store=AgentIdStore(store_path)).new_agent_id("/wt")
    backups = sorted(store_path.parent.glob("agent_ids.json.corrupt-*"))
    assert [b.read_text() for b in backups] == ["corrupt 2", "corrupt 3", "corrupt 4"]


def test_bad_entries_are_skipped_good_ones_kept(store_path):
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps({"counters": {"/wt": 4, "/bad": "7", "/flag": True}}))
    registry = AgentRegistry(id_store=AgentIdStore(store_path))
    assert registry.new_agent_id("/wt") == "a5"
    assert registry.new_agent_id("/bad") == "a1"


def test_a_lost_file_never_goes_below_a_live_agent(store_path):
    registry = AgentRegistry(id_store=AgentIdStore(store_path))
    registry.get("/wt", "a7")
    registry.register("/wt", "a9")
    assert registry.new_agent_id("/wt") == "a10"


def test_a_file_deleted_mid_run_keeps_ids_monotonic(store_path):
    registry = AgentRegistry(id_store=AgentIdStore(store_path))
    _ids(registry, "/wt", 3)
    store_path.unlink()
    assert registry.new_agent_id("/wt") == "a4"


def test_an_unwritable_store_still_allocates(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    registry = AgentRegistry(id_store=AgentIdStore(blocker / "agent_ids.json"))
    with caplog.at_level(logging.WARNING, logger="lazyagent.engine.registry"):
        assert _ids(registry, "/wt", 2) == ["a1", "a2"]
    assert "unavailable" in caplog.text


def _allocate_many(path: str, key: str, n: int, queue) -> None:
    store = AgentIdStore(path)
    queue.put([store.allocate(key) for _ in range(n)])


def test_concurrent_processes_never_get_the_same_id(store_path):
    store_path.parent.mkdir(parents=True)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_allocate_many, args=(str(store_path), "/wt", 50, queue))
        for _ in range(4)
    ]
    for p in procs:
        p.start()
    results = [queue.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    allocated = [n for r in results for n in r]
    assert sorted(allocated) == list(range(1, 201))
