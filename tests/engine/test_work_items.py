"""External feeders (proposal 0013, I6): work_items_upsert / work_items_close,
dedupe, caps, expiry, the "not started" counter and ticket linking, priority
on rows and inbox items, waiting items parked, projects (grouping,
"waits on", the continue_while_review wake) and restarts."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from lazyagent.config import Config, StatusConfig, load_config
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.engine.trackers import TrackerConfig, TrackerHub, TrackerTask
from lazyagent.engine.work_items import WorkItems, work_items_path
from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo, ticket_id
from tests.engine.fakes import add_session, make_engine, worktree

pytestmark = pytest.mark.asyncio

NOW = 1_700_000_000.0
FEAT = "/tmp/repo-feat"
SHOP = "/tmp/repo-shop"
SHOP_KEY = "repo:SHOP-12-checkout"
T = "https://tracker.example.com/browse/"
GREEN = [CiCheck("tests", "COMPLETED", "SUCCESS")]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(*branches: str, status: StatusConfig | None = None):
    trees = [worktree("/tmp/repo")]
    trees += [worktree(f"/tmp/repo-{i}", b, is_main=False) for i, b in enumerate(branches)]
    engine = make_engine(trees, config=Config(status=status or StatusConfig()))
    clock = Clock()
    engine.status.clock = clock
    return engine, clock


def _shop_engine(**status):
    engine = make_engine(
        [worktree("/tmp/repo"), worktree(SHOP, "SHOP-12-checkout", is_main=False),
         worktree(FEAT, "feat", is_main=False)],
        config=Config(status=StatusConfig(**status)),
    )
    clock = Clock()
    engine.status.clock = clock
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
        add_session(engine, session)
    return engine.get_session(path, agent_id)


def _orchestrator(engine):
    session = _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    return session


async def _upsert(engine, url, title="Task", source="planner", **kw):
    return await EngineMethods(engine).work_items_upsert(
        {"url": url, "title": title, "source": source, **kw}
    )


def _row(engine, key):
    return next((r for r in engine.status.list(include_done=True)["rows"] if r["key"] == key),
                None)


async def _pass(engine):
    engine.status.flush()
    await engine.wait_idle()
    engine.status.flush()
    await engine.wait_idle()


# --- Upsert, dedupe, close -----------------------------------------------------


async def test_upsert_dedupes_by_url_and_replaces_the_fields():
    engine, clock = _engine()
    first = await _upsert(engine, T + "SHOP-1", "Old title", priority="high",
                          waiting={"reason": "later"})
    assert first["created"] is True and first["key"] is None
    clock.now += 60
    second = await _upsert(engine, T + "SHOP-1", "New title")
    assert second["created"] is False
    item = second["item"]
    # Left out: back to the defaults (medium, no waiting).
    assert (item["title"], item["priority"], item["waiting"]) == ("New title", "medium", None)
    assert (item["created_at"], item["updated_at"]) == (NOW, NOW + 60)
    assert list(engine.work_items.records) == [T + "SHOP-1"]

    closed = await EngineMethods(engine).work_items_close({"url": T + "SHOP-1"})
    assert closed["closed"] is True and closed["item"]["title"] == "New title"
    again = await EngineMethods(engine).work_items_close({"url": T + "SHOP-1"})
    assert again == {"url": T + "SHOP-1", "closed": False, "item": None}


@pytest.mark.parametrize("params, message", [
    ({"title": "x", "source": "p"}, "url is required"),
    ({"url": "ftp://x/1", "title": "x", "source": "p"}, "http"),
    ({"url": T + "1", "source": "p"}, "title is required"),
    ({"url": T + "1", "title": "x"}, "source is required"),
    ({"url": T + "1", "title": "x", "source": "a b"}, "source is required"),
    ({"url": T + "1", "title": "x", "source": "p", "priority": "urgent"}, "priority"),
    ({"url": T + "1", "title": "x", "source": "p", "depends_on": [T + "1"]}, "itself"),
    ({"url": T + "1", "title": "x", "source": "p", "expires_at": NOW - 1}, "past"),
    ({"url": T + "1", "title": "x", "source": "p", "waiting": {"until": "soon"}}, "until"),
    ({"url": T + "1", "title": "x", "source": "p", "project": {"title": "no id"}}, "project.id"),
])
async def test_upsert_refuses_bad_fields(params, message):
    engine, _ = _engine()
    with pytest.raises(ValueError, match=message):
        await EngineMethods(engine).work_items_upsert(params)
    assert engine.work_items.records == {}


async def test_open_items_are_capped_per_source():
    engine, _ = _engine(status=StatusConfig(max_open_per_source=2))
    await _upsert(engine, T + "1")
    await _upsert(engine, T + "2")
    with pytest.raises(ValueError, match="max_open_per_source = 2"):
        await _upsert(engine, T + "3")
    # Updating a known one, and another source, are fine.
    await _upsert(engine, T + "2", "renamed")
    await _upsert(engine, T + "3", source="script")
    assert len(engine.work_items.records) == 3


async def test_an_item_expires_at_expires_at():
    engine, clock = _engine()
    await _upsert(engine, T + "SHOP-5", expires_at=NOW + 600)
    assert engine.status.list()["counts"]["not_started"] == 1
    clock.now += 600
    assert engine.status.list()["counts"]["not_started"] == 0
    assert engine.work_items.records == {}


# --- Not started and ticket linking ------------------------------------------


async def test_a_card_with_no_branch_is_not_started_and_joins_the_row_once_one_exists():
    engine, _ = _engine()
    await _upsert(engine, T + "SHOP-12", "Checkout flow")
    result = engine.status.list()
    assert result["counts"]["not_started"] == 1
    [item] = result["not_started_items"]
    assert (item["title"], item["ticket"], item["source"]) == ("Checkout flow", "SHOP-12", "planner")
    assert result["rows"] == []

    engine.worktree_service.worktrees.append(worktree(SHOP, "SHOP-12-checkout", is_main=False))
    result = engine.status.list()
    assert result["counts"]["not_started"] == 0
    [row] = result["rows"]
    assert row["key"] == SHOP_KEY
    assert row["title"] == "Checkout flow"
    assert T + "SHOP-12" in row["urls"]
    assert row["feeder"]["source"] == "planner"


async def test_a_local_branch_with_no_worktree_counts_as_started():
    engine, _ = _engine()
    await _upsert(engine, T + "SHOP-12", "Checkout flow")
    engine.status._commit_times = {"SHOP-12-old-try": NOW}
    assert engine.status.list()["counts"]["not_started"] == 0


async def test_a_custom_ticket_pattern_links_lowercase_branches():
    engine, _ = _engine("shop_12_checkout", status=StatusConfig(ticket_pattern=r"shop_(\d+)"))
    await _upsert(engine, "https://board.example.com/cards/9", "Checkout", ticket="12")
    assert _row(engine, "repo:shop_12_checkout")["title"] == "Checkout"
    # The default pattern would not have linked it.
    assert ticket_id("shop_12_checkout") is None


async def test_a_record_links_by_its_branch_and_a_ticket_in_another_case():
    engine, _ = _engine("feat-login", "SHOP-7-x")
    await _upsert(engine, "https://board.example.com/c/1", "Login", branch="feat-login")
    await _upsert(engine, "https://board.example.com/c/2", "Seven", ticket="shop-7")
    assert _row(engine, "repo:feat-login")["title"] == "Login"
    assert _row(engine, "repo:SHOP-7-x")["title"] == "Seven"


async def test_ticket_pattern_config_is_read_and_a_bad_one_ignored(tmp_path):
    (tmp_path / ".lazyagent.toml").write_text(
        '[status]\nticket_pattern = "x-(\\\\d+)"\nmax_open_per_source = 3\n'
    )
    config = load_config(str(tmp_path))
    assert (config.status.ticket_pattern, config.status.max_open_per_source) == (r"x-(\d+)", 3)
    (tmp_path / ".lazyagent.toml").write_text('[status]\nticket_pattern = "("\n')
    assert load_config(str(tmp_path)).status.ticket_pattern is None


async def test_feeder_and_tracker_tasks_count_once_per_url():
    engine, _ = _engine()

    class Adapter:
        name = "fake"

        def key_pattern(self):
            return None

        async def fetch(self, keys):
            return []

        async def assigned_to_me(self):
            return [TrackerTask("SHOP-12", T + "SHOP-12", "Checkout", "To Do"),
                    TrackerTask("SHOP-13", T + "SHOP-13", "Search", "To Do", priority="low")]

    engine.trackers = TrackerHub([(Adapter(), TrackerConfig("fake", enabled=True))])
    await engine.trackers.poll([], force=True)
    await _upsert(engine, T + "SHOP-12", "Checkout flow", priority="high")
    result = engine.status.list()
    assert result["counts"]["not_started"] == 2
    assert [(i["url"], i["source"], i["priority"]) for i in result["not_started_items"]] == [
        (T + "SHOP-12", "planner", "high"), (T + "SHOP-13", "tracker:fake", "low"),
    ]


async def test_with_no_feeder_status_list_has_no_new_fields():
    engine, _ = _engine("feat")
    result = engine.status.list()
    assert "not_started_items" not in result
    assert not {"project", "depends_on", "waits_on", "feeder"} & set(result["rows"][0])


# --- Priority -----------------------------------------------------------------


async def test_priority_orders_rows_and_inbox_items_after_blocking():
    engine, clock = _engine("ABC-1-a", "ABC-2-b", "ABC-3-c")
    # Three agents finished their turn (deep items), oldest first: a, b, c.
    for i in range(3):
        _agent(engine, f"/tmp/repo-{i}", f"a{i}", "completed")
        engine.status.flush()
        clock.now += 60
    await _upsert(engine, T + "ABC-3", "C", priority="high")
    await _upsert(engine, T + "ABC-1", "A", priority="low")
    rows = engine.status.list()["rows"]
    assert [r["title"] for r in rows] == ["C", "ABC-2-b", "A"]
    assert [r["priority"] for r in rows] == ["high", "medium", "low"]
    items = engine.actionables.list()["items"]
    assert [(i["work_item"], i["priority"]) for i in items] == [
        ("repo:ABC-3-c", "high"), ("repo:ABC-2-b", "medium"), ("repo:ABC-1-a", "low"),
    ]
    # Blocking a live agent still comes first, whatever the priority.
    _agent(engine, "/tmp/repo-0", "a0", "waiting_for_approval", "Bash")
    items = engine.actionables.list()["items"]
    assert items[0]["work_item"] == "repo:ABC-1-a" and items[0]["blocks_agent"]
    assert "score" not in items[0]


async def test_a_tracker_may_supply_the_priority():
    engine, _ = _engine("SHOP-9-x")

    class Adapter:
        name = "fake"

        def key_pattern(self):
            return None

        async def fetch(self, keys):
            return [TrackerTask("SHOP-9", T + "SHOP-9", "Nine", "To Do", priority="high")]

        async def assigned_to_me(self):
            raise NotImplementedError

    engine.trackers = TrackerHub([(Adapter(), TrackerConfig("fake", enabled=True))])
    await engine.trackers.poll(["SHOP-9-x"], force=True)
    assert _row(engine, "repo:SHOP-9-x")["priority"] == "high"
    # The feeder's wins.
    await _upsert(engine, T + "SHOP-9", "Nine", priority="low")
    assert _row(engine, "repo:SHOP-9-x")["priority"] == "low"


# --- Waiting ------------------------------------------------------------------


async def test_waiting_parks_the_row_until_the_feeder_clears_it():
    engine, _ = _shop_engine()
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board", waiting={})
    row = _row(engine, SHOP_KEY)
    assert (row["zone"], row["state"], row["rule"]) == ("parked", "waiting (board)", 7)
    assert row["gesture"]["by"] == "feeder:board" and row["gesture"]["waiting"] is True
    assert engine.status.list()["counts"]["parked"] == 1

    await _upsert(engine, T + "SHOP-12", "Checkout", source="board",
                  waiting={"reason": "on hold for design"})
    assert _row(engine, SHOP_KEY)["state"] == "waiting: on hold for design"

    await _upsert(engine, T + "SHOP-12", "Checkout", source="board")
    row = _row(engine, SHOP_KEY)
    assert row["zone"] != "parked" and row["gesture"] is None


async def test_waiting_ends_at_until_and_the_same_value_does_not_rearm_it():
    engine, clock = _shop_engine()
    wait = {"until": NOW + 3600}
    await _upsert(engine, T + "SHOP-12", "Checkout", waiting=wait)
    assert _row(engine, SHOP_KEY)["zone"] == "parked"
    clock.now += 3600
    assert _row(engine, SHOP_KEY)["zone"] != "parked"
    # The planner sends the same thing on its next tick: still over.
    await _upsert(engine, T + "SHOP-12", "Checkout", waiting=wait)
    assert _row(engine, SHOP_KEY)["zone"] != "parked"
    # A new value parks it again.
    await _upsert(engine, T + "SHOP-12", "Checkout", waiting={"until": NOW + 9000})
    assert _row(engine, SHOP_KEY)["zone"] == "parked"


async def test_waiting_until_an_event():
    engine, _ = _shop_engine()
    await _upsert(engine, T + "SHOP-12", "Checkout", waiting={"until": "change"})
    assert _row(engine, SHOP_KEY)["zone"] == "parked"
    engine.worktree_service._pr_infos[SHOP] = PrInfo(
        3, "Checkout", "OPEN", GREEN, "https://example.com/pull/3")
    assert _row(engine, SHOP_KEY)["zone"] != "parked"


async def test_the_users_unpark_wins_until_the_feeder_changes_waiting():
    engine, _ = _shop_engine()
    methods = EngineMethods(engine)
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board", waiting={})
    await methods.status_gesture({"key": SHOP_KEY, "kind": "clear"})
    assert _row(engine, SHOP_KEY)["zone"] != "parked"
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board", waiting={})
    assert _row(engine, SHOP_KEY)["zone"] != "parked"
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board",
                  waiting={"reason": "blocked upstream"})
    assert _row(engine, SHOP_KEY)["state"] == "waiting: blocked upstream"


async def test_the_users_own_park_wins_over_the_boards():
    engine, _ = _shop_engine()
    await EngineMethods(engine).status_gesture(
        {"key": SHOP_KEY, "kind": "park", "until": "indefinite", "text": "mine"})
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board", waiting={})
    row = _row(engine, SHOP_KEY)
    assert (row["state"], row["gesture"]["by"]) == ("parked: mine", "user")


async def test_a_waiting_items_question_follows_parked_routing():
    engine, _ = _shop_engine()
    await _upsert(engine, T + "SHOP-12", "Checkout", source="board", waiting={})
    _agent(engine, SHOP, "a1", "waiting_for_user", "Which db?")
    # A Now item takes the row out of Parked (R4c).
    assert (_row(engine, SHOP_KEY)["zone"], _row(engine, SHOP_KEY)["level"]) == ("inbox", "now")
    engine.registry.states[SHOP].pop("a1")
    engine._agents[SHOP].pop("a1")
    _orchestrator(engine)
    _agent(engine, SHOP, "a2", "waiting_for_user", "Which db?")
    engine.status.flush()
    row = _row(engine, SHOP_KEY)
    assert (row["zone"], row["holder"], row["state"]) == ("parked", "orchestrator", "waiting (board)")


async def test_waiting_items_are_not_counted_as_not_started():
    engine, _ = _engine()
    await _upsert(engine, T + "SHOP-12", waiting={"reason": "later"})
    assert engine.status.list()["counts"]["not_started"] == 0


# --- Projects -----------------------------------------------------------------

PROJECT = {"id": "shop", "title": "Shop", "policy": {"continue_while_review": True}}


async def test_rows_carry_their_project_and_what_they_wait_on():
    engine, _ = _shop_engine()
    await _upsert(engine, T + "SHOP-11", "Cart", project=PROJECT)
    await _upsert(engine, T + "SHOP-12", "Checkout", project={"id": "shop", "title": "Shop"},
                  depends_on=[T + "SHOP-11", T + "SHOP-1"])
    row = _row(engine, SHOP_KEY)
    # The policy stays the project's when an item leaves it out.
    assert row["project"] == PROJECT
    assert row["depends_on"] == [T + "SHOP-11", T + "SHOP-1"]
    # SHOP-1 is unknown (closed): only the open one is waited on.
    assert row["waits_on"] == [{"url": T + "SHOP-11", "title": "Cart", "state": "open"}]
    from lazyagent.engine.status import format_table

    assert "waits on Cart" in format_table(engine.status.list(), NOW)
    await EngineMethods(engine).work_items_close({"url": T + "SHOP-11"})
    assert _row(engine, SHOP_KEY)["waits_on"] == []


async def test_a_dependency_with_a_row_is_open_in_review_or_merged():
    engine, _ = _shop_engine()
    await _upsert(engine, T + "SHOP-11", "Cart", branch="feat")
    await _upsert(engine, T + "SHOP-12", "Checkout", depends_on=[T + "SHOP-11"])
    assert _row(engine, SHOP_KEY)["waits_on"][0]["state"] == "open"
    pr = PrInfo(4, "Cart", "OPEN", GREEN, "https://example.com/pull/4")
    engine.worktree_service._pr_infos[FEAT] = pr
    assert _row(engine, SHOP_KEY)["waits_on"] == [
        {"url": T + "SHOP-11", "title": "Cart", "state": "review"}]
    engine.worktree_service._pr_infos[FEAT] = PrInfo(4, "Cart", "MERGED", GREEN)
    assert _row(engine, SHOP_KEY)["waits_on"] == []


async def _project(engine, policy=True):
    project = {"id": "shop", "title": "Shop", "policy": {"continue_while_review": policy}}
    await _upsert(engine, T + "SHOP-11", "Cart", branch="feat", project=project)
    await _upsert(engine, T + "SHOP-20", "Refunds", project=project, depends_on=[T + "SHOP-11"])


async def _review(engine):
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        4, "Cart", "OPEN", GREEN, "https://example.com/pull/4")
    await _pass(engine)


@pytest.mark.parametrize("presence, words", [
    ("away", "The user is away: start it within the spawn caps"),
    ("present", "The user is present: propose it to them"),
])
async def test_continue_while_review_wakes_the_orchestrator_once(presence, words):
    engine, _ = _shop_engine()
    orch = _orchestrator(engine)
    engine.first_responder.presence.set(presence)
    engine.first_responder.presence.connected = lambda: True
    await _project(engine)
    await _pass(engine)
    orch.send_input.assert_not_awaited()
    assert engine.status.list()["not_started_items"][0]["startable"] is False

    await _review(engine)
    text = orch.send_input.await_args.args[0]
    assert text.startswith(
        f"Project Shop: task Refunds ({T}SHOP-20) can start: its dependencies are merged "
        "or in review (continue_while_review).")
    assert words in text
    assert engine.status.list()["not_started_items"][0]["startable"] is True
    # Once.
    await _pass(engine)
    assert orch.send_input.await_count == 1


async def test_no_wake_without_the_policy_or_without_an_orchestrator():
    engine, _ = _shop_engine()
    orch = _orchestrator(engine)
    await _project(engine, policy=False)
    await _review(engine)
    orch.send_input.assert_not_awaited()

    engine, _ = _shop_engine()
    await _project(engine)
    await _review(engine)
    # Not woken yet: it waits for an orchestrator.
    assert engine.work_items.records[T + "SHOP-20"].get("woke") is None
    orch = _orchestrator(engine)
    await _pass(engine)
    orch.send_input.assert_awaited_once()


async def test_the_wake_survives_a_restart(tmp_path):
    engine, _ = _shop_engine()
    path = tmp_path / "items.json"
    engine.work_items.open(path)
    orch = _orchestrator(engine)
    await _project(engine)
    await _review(engine)
    orch.send_input.assert_awaited_once()
    engine.work_items.save()

    engine2, _ = _shop_engine()
    engine2.work_items.open(path)
    orch2 = _orchestrator(engine2)
    await _review(engine2)
    orch2.send_input.assert_not_awaited()


# --- Persistence ----------------------------------------------------------------


async def test_records_survive_a_restart_in_a_private_file(tmp_path):
    store = WorkItems(clock=lambda: NOW)
    path = work_items_path("/tmp/repo")
    assert str(path).startswith(os.environ["XDG_STATE_HOME"])
    store.open(path)
    store.upsert({"url": T + "SHOP-12", "title": "Checkout", "source": "board",
                  "priority": "high", "project": PROJECT,
                  "waiting": {"reason": "later"}}, NOW)
    store.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text())
    assert data["version"] == 1 and T + "SHOP-12" in data["records"]

    again = WorkItems(clock=lambda: NOW)
    again.open(path)
    item = again.public(again.records[T + "SHOP-12"])
    assert (item["priority"], item["project"], item["waiting"]["reason"]) == (
        "high", PROJECT, "later")


async def test_the_engine_keeps_records_across_a_restart():
    engine, _ = _shop_engine()
    engine.work_items.open(work_items_path(engine.repo_root))
    await _upsert(engine, T + "SHOP-12", "Checkout", waiting={})
    engine.work_items.save()
    engine2, _ = _shop_engine()
    engine2.work_items.open(work_items_path(engine2.repo_root))
    assert _row(engine2, SHOP_KEY)["state"] == "waiting (planner)"


async def test_expired_records_are_dropped_on_load(tmp_path):
    path = tmp_path / "w.json"
    store = WorkItems(clock=lambda: NOW)
    store.open(path)
    store.upsert({"url": T + "1", "title": "x", "source": "p", "expires_at": NOW + 1}, NOW)
    store.save()
    later = WorkItems(clock=lambda: NOW + 2)
    later.open(path)
    assert later.records == {}
