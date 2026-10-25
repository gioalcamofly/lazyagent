"""The engine's auto-hold of the orchestrator's workers (I4d), fixed: a
worker that reports it works on in the background is not "finished", a
release is not undone by the same idle turn, and a removed or stopped item
is never held nor woken for."""

from __future__ import annotations

import pytest

from lazyagent.engine.claims import NOTHING_TO_RELEASE
from lazyagent.engine.methods import EngineMethods
from lazyagent.engine.overlay import MAX_REPORT_FOR, REPORT_FOR, report_until
from lazyagent.engine.registry import ORCHESTRATOR_KEY
from lazyagent.models import PrInfo
from tests.engine.fakes import worktree
from tests.engine.test_home_polish import FEAT, KEY, NOW, _agent, _engine, _orchestrator, _pass, _row

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))


def _worker(**orchestrator):
    """An orchestrator, and its worker a1 on FEAT, running."""
    engine, clock = _engine(**orchestrator)
    orch = _orchestrator(engine)
    engine.overlay.set_spawned(KEY, "a1", "orchestrator", NOW)
    engine._tokens["tok-a1"] = (FEAT, "a1")
    _agent(engine, FEAT, "a1", "running")
    return engine, clock, orch


async def _report(engine, **params):
    return await EngineMethods(engine).status_report(
        {"note": "sampling in background until ~09:40", "ball": "agent", "caller": "tok-a1",
         **params}
    )


def _kinds(engine):
    return [i["kind"] for i in engine.actionables.list()["items"]]


def _woken(orch):
    return [c.args[0] for c in orch.send_input.await_args_list]


# --- Bug 1: background work is not "finished" --------------------------------


async def test_a_background_report_suppresses_done_and_the_auto_hold():
    engine, _, orch = _worker()
    await _pass(engine)
    await _report(engine, until="45m")
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    row = _row(engine)
    assert row["hold"] is None
    assert (row["ball"], row["zone"], row["state"]) == (
        "agent", "in_flight", "agent a1 · sampling in background until ~09:40",
    )
    assert "done" not in _kinds(engine)
    assert _woken(orch) == []


async def test_a_fresh_turn_without_a_report_lets_done_fire_again():
    engine, _, orch = _worker()
    await _report(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is None
    # The orchestrator (or the sampler's result) sends it input: a new turn...
    _agent(engine, FEAT, "a1", "running")
    await _pass(engine)
    # ...that ends with no new report: finished, held for the orchestrator.
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True
    assert _woken(orch)[0].startswith(f"Item {KEY}: agent a1 (spawned by you) — agent finished")


async def test_a_fresh_turn_with_a_new_report_keeps_it_working():
    engine, _, orch = _worker()
    await _report(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    _agent(engine, FEAT, "a1", "running")
    await _pass(engine)
    await _report(engine, note="still sampling")
    _agent(engine, FEAT, "a1", "waiting")
    await _pass(engine)
    assert (_row(engine)["hold"], _row(engine)["state"]) == (None, "agent a1 · still sampling")
    assert _woken(orch) == []


async def test_a_short_run_between_two_passes_still_ends_the_report():
    """The agent.status events count a run the passes never saw."""
    engine, _, _ = _worker()
    await _report(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    for status in ("running", "completed"):
        engine.status.saw_status(FEAT, "a1", status)
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True


async def test_the_report_lapses_at_its_until():
    engine, clock, orch = _worker()
    result = await _report(engine, until="45m")
    assert result["report"]["until"] == NOW + 45 * 60
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is None
    clock.now += 45 * 60 - 1
    await _pass(engine)
    assert _row(engine)["hold"] is None
    clock.now += 1
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True
    assert len(_woken(orch)) == 1


async def test_the_report_lapses_by_default_after_two_hours():
    engine, clock, _ = _worker()
    result = await _report(engine)
    assert result["report"]["until"] == NOW + REPORT_FOR
    _agent(engine, FEAT, "a1", "completed")
    clock.now += REPORT_FOR - 60
    await _pass(engine)
    assert _row(engine)["hold"] is None
    clock.now += 60
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True


async def test_without_an_orchestrator_the_lapsed_report_reaches_the_user():
    engine, clock = _engine()
    engine._tokens["tok-a1"] = (FEAT, "a1")
    _agent(engine, FEAT, "a1", "running")
    await _report(engine, until=NOW + 600)
    _agent(engine, FEAT, "a1", "completed")
    assert _row(engine)["zone"] == "in_flight"
    clock.now += 600
    row = _row(engine)
    assert (row["level"], row["state"]) == ("deep", "review: agent finished")


async def test_others_and_stalled_suppress_done_too():
    for ball in ("others", "stalled"):
        engine, _, orch = _worker()
        await _report(engine, ball=ball, note="waiting for QA")
        _agent(engine, FEAT, "a1", "completed")
        await _pass(engine)
        assert (_row(engine)["hold"], _row(engine)["ball"]) == (None, ball)
        assert "done" not in _kinds(engine)


async def test_others_and_stalled_have_no_default_expiry_but_take_until():
    engine, clock, _ = _worker()
    result = await _report(engine, ball="others", note="waiting for QA")
    assert result["report"]["until"] is None
    _agent(engine, FEAT, "a1", "completed")
    clock.now += 8 * 3600
    await _pass(engine)
    assert (_row(engine)["hold"], _row(engine)["ball"]) == (None, "others")
    # Until its next turn ends with no new report.
    _agent(engine, FEAT, "a1", "running")
    await _pass(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True

    engine, clock, _ = _worker()
    result = await _report(engine, ball="stalled", note="blocked on a key", until="1h")
    assert result["report"]["until"] == NOW + 3600
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is None
    clock.now += 3600
    await _pass(engine)
    assert _row(engine)["hold"]["auto"] is True


async def test_a_reviewers_changes_requested_still_surface_during_background_work():
    engine, _, orch = _worker()
    engine.worktree_service._pr_infos[FEAT] = PrInfo(
        7, "Feat", "OPEN", [], "https://example.com/pull/7", review_decision="CHANGES_REQUESTED",
    )
    await _report(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert engine.actionables.found(f"Review:{FEAT}:7") is not None
    assert _row(engine)["hold"]["auto"] is True
    assert "done" not in _kinds(engine)
    # The orchestrator's worker: its review is held for it, as before.
    assert _woken(orch)[0].startswith(f"Item {KEY}: agent a1 (spawned by you) — changes requested")


async def test_report_until_takes_a_time_or_a_duration_up_to_eight_hours():
    assert report_until(None, NOW) is None
    assert report_until("45m", NOW) == NOW + 2700
    assert report_until("1h30m", NOW) == NOW + 5400
    assert report_until("90s", NOW) == NOW + 90
    assert report_until(NOW + 60, NOW) == NOW + 60
    assert report_until("8h", NOW) == NOW + MAX_REPORT_FOR
    for bad, match in (("9h", "at most 8 h"), (NOW - 1, "already past"), ("soon", "duration"),
                       ("", "duration"), (True, "Unix time")):
        with pytest.raises(ValueError, match=match):
            report_until(bad, NOW)


async def test_the_orchestrators_report_takes_no_until():
    engine, _, _ = _worker()
    with pytest.raises(ValueError, match="until are for a worker"):
        await EngineMethods(engine).status_report(
            {"note": "x", "until": "1h", "caller": "tok-orch"}
        )


async def test_a_release_is_not_retriggered_by_the_same_idle_turn():
    engine, clock, orch = _worker()
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is not None
    await EngineMethods(engine).orchestrator_release(
        {"key": KEY, "outcome": "done", "note": "verified", "caller": "tok-orch"}
    )
    # The same idle turn shows a new occurrence of its item (it went and
    # came back): not held again.
    clock.now += 60
    _agent(engine, FEAT, "a1", "possibly_hanged")
    await _pass(engine)
    _agent(engine, FEAT, "a1", "waiting")
    await _pass(engine)
    assert _row(engine)["hold"] is None
    assert len(_woken(orch)) == 1
    # A new run that ends: held again.
    clock.now += 60
    _agent(engine, FEAT, "a1", "running")
    await _pass(engine)
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is not None


# --- Bug 2: stale holds on removed items --------------------------------------


def _remove(engine):
    engine.worktree_service.worktrees = [
        w for w in engine.worktree_service.worktrees if w.path != FEAT
    ]
    engine.registry.states.pop(FEAT, None)
    engine._agents.pop(FEAT, None)
    engine.status.mark_dirty()


async def test_a_removed_worktree_ends_a_pending_hold_and_its_wake_line():
    engine, _, orch = _worker()
    # The orchestrator is busy: the wake line waits for its idle.
    _agent(engine, ORCHESTRATOR_KEY, "", "running")
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is not None
    assert _woken(orch) == []
    # In its turn it stops the agent and removes the worktree.
    _remove(engine)
    _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    await _pass(engine)
    assert engine.claims.holds == {}
    assert _woken(orch) == []


async def test_no_hold_for_an_item_whose_worktree_is_gone():
    engine, _, orch = _worker()
    _agent(engine, FEAT, "a1", "completed")
    _remove(engine)
    await _pass(engine)
    assert engine.claims.holds == {} and _woken(orch) == []


async def test_a_stopped_worker_ends_its_auto_hold_quietly():
    engine, _, orch = _worker()
    _agent(engine, ORCHESTRATOR_KEY, "", "running")
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    assert _row(engine)["hold"] is not None
    engine.registry.states[FEAT].pop("a1")
    engine._agents.pop(FEAT, None)
    _agent(engine, ORCHESTRATOR_KEY, "", "waiting")
    await _pass(engine)
    assert engine.claims.holds == {} and _woken(orch) == []
    assert _row(engine)["hold"] is None


async def test_release_is_idempotent_and_an_unknown_key_is_still_an_error():
    engine, _, _ = _worker()
    _agent(engine, FEAT, "a1", "completed")
    await _pass(engine)
    methods = EngineMethods(engine)
    params = {"key": KEY, "outcome": "done", "note": "ok", "caller": "tok-orch"}
    first = await methods.orchestrator_release(params)
    assert first["hold"]["auto"] is True
    again = await methods.orchestrator_release(params)
    assert again == {"key": KEY, "outcome": NOTHING_TO_RELEASE, "claim": None}
    # Its worktree removed: a tombstone row, still nothing to release.
    _remove(engine)
    await _pass(engine)
    assert (await methods.orchestrator_release(params))["outcome"] == NOTHING_TO_RELEASE
    with pytest.raises(ValueError, match="No work item 'repo:nope'"):
        await methods.orchestrator_release({**params, "key": "repo:nope"})
    with pytest.raises(ValueError, match="outcome must be one of"):
        await methods.orchestrator_release({**params, "outcome": "maybe"})


async def test_main_checkout_is_never_mistaken_for_a_removed_worktree():
    engine, _, _ = _worker()
    assert worktree("/tmp/repo").path in engine.claims._paths()
