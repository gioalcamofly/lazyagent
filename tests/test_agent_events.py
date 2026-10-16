"""Tests for the agent event bus and the engine's publishing of it."""

from __future__ import annotations

import asyncio

import pytest

from lazyagent.agent_events import (
    EXITED,
    STATUS_GROUPS,
    AgentEventBus,
    make_filter,
    resolve_statuses,
)
from lazyagent.engine.session import SessionExited, StatusChanged
from lazyagent.models import AgentStatus, LifecycleConfidence
from tests.engine.fakes import make_engine, no_pty, worktree


def _publish(bus, status, *, path="/wt", agent_id="a1", previous="running"):
    return bus.publish(
        worktree_path=path,
        agent_id=agent_id,
        status=status,
        previous_status=previous,
    )


class TestResolveStatuses:
    def test_none_means_no_filter(self):
        assert resolve_statuses(None) is None
        assert resolve_statuses([]) is None

    def test_expands_groups_and_raw_values(self):
        assert resolve_statuses("blocked") == {
            "waiting_for_approval",
            "waiting_for_user",
        }
        assert resolve_statuses(["idle", "failed"]) == {"waiting", "failed"}

    def test_not_running_excludes_only_running(self):
        group = STATUS_GROUPS["not_running"]
        assert "running" not in group
        assert EXITED in group
        assert "possibly_hanged" in group

    def test_unknown_name_lists_accepted_values(self):
        with pytest.raises(ValueError, match="Accepted: .*blocked"):
            resolve_statuses("sleeping")


class TestAgentEventBus:
    def test_seq_is_monotonic_and_cursor_tracks_it(self):
        bus = AgentEventBus()
        assert bus.cursor == 0
        first = _publish(bus, "running")
        second = _publish(bus, "waiting")
        assert (first.seq, second.seq, bus.cursor) == (1, 2, 2)

    def test_since_returns_only_newer_matching_events(self):
        bus = AgentEventBus()
        _publish(bus, "running")
        _publish(bus, "waiting", agent_id="a2")
        _publish(bus, "waiting")
        events, truncated = bus.since(1, make_filter(agent_id="a1"))
        assert [e.seq for e in events] == [3]
        assert truncated is False

    def test_since_reports_evicted_events(self):
        bus = AgentEventBus(maxlen=2)
        for status in ("running", "waiting", "running"):
            _publish(bus, status)
        events, truncated = bus.since(0)
        assert [e.seq for e in events] == [2, 3]
        assert truncated is True
        assert bus.since(1)[1] is False

    def test_filter_matches_worktree_and_status(self):
        bus = AgentEventBus()
        event = _publish(bus, "waiting")
        assert make_filter("/wt", "a1", frozenset({"waiting"}))(event)
        assert not make_filter("/other")(event)
        assert not make_filter(statuses=frozenset({"failed"}))(event)
        # agent_id "" (the orchestrator) is a real id, not "any".
        assert not make_filter(agent_id="")(event)

    @pytest.mark.asyncio
    async def test_wait_returns_immediately_when_already_matched(self):
        bus = AgentEventBus()
        _publish(bus, "waiting")
        events, _ = await bus.wait(0, timeout=10)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_wait_wakes_on_matching_publish_only(self):
        bus = AgentEventBus()
        match = make_filter(statuses=frozenset({"waiting"}))
        waiter = asyncio.create_task(bus.wait(0, match, timeout=5))
        await asyncio.sleep(0)
        _publish(bus, "running")          # wakes the waiter, doesn't match
        await asyncio.sleep(0)
        assert not waiter.done()
        _publish(bus, "waiting")
        events, _ = await asyncio.wait_for(waiter, 1)
        assert [e.status for e in events] == ["waiting"]

    @pytest.mark.asyncio
    async def test_wait_times_out_with_empty_list(self):
        bus = AgentEventBus()
        events, truncated = await bus.wait(0, timeout=0.01)
        assert events == [] and truncated is False


class TestEnginePublishesEvents:
    """Session reports reach the event log through the engine."""

    @staticmethod
    async def _engine_with_agent(monkeypatch):
        no_pty(monkeypatch)
        engine = make_engine([worktree("/wt", "feat", is_main=False)])
        await engine.spawn_agent("/wt")
        return engine, engine.get_session("/wt", "a1")

    @staticmethod
    def _report(session, status, detail=""):
        session._emit(
            StatusChanged("/wt", "a1", status, LifecycleConfidence.LOW, detail, None)
        )

    @pytest.mark.asyncio
    async def test_status_change_is_published_once(self, monkeypatch):
        engine, session = await self._engine_with_agent(monkeypatch)
        self._report(session, AgentStatus.RUNNING)
        # Detail-only update: same status, no new event.
        self._report(session, AgentStatus.RUNNING, "using Bash")
        self._report(session, AgentStatus.WAITING, "waiting")
        events, _ = engine.events.since(0)
        assert [(e.previous_status, e.status) for e in events] == [
            ("no_agent", "running"),
            ("running", "waiting"),
        ]
        assert events[1].detail == "waiting"

    @pytest.mark.asyncio
    async def test_exit_is_published_once(self, monkeypatch):
        engine, session = await self._engine_with_agent(monkeypatch)
        self._report(session, AgentStatus.WAITING)
        session._emit(SessionExited("/wt", "a1", 0))
        # A stop racing the exit must not publish again.
        with pytest.raises(ValueError):
            engine.stop_agent("/wt", "a1")
        engine.registry.drop("/wt", "a1")
        events, _ = engine.events.since(0)
        assert [(e.previous_status, e.status) for e in events] == [
            ("no_agent", "waiting"),
            ("waiting", EXITED),
        ]


class TestTruncation:
    def test_events_of_other_kinds_do_not_count_as_a_gap(self):
        from lazyagent.engine.events import EventBus

        bus = EventBus(maxlen=2)
        _publish(bus, "running")                 # seq 1
        bus.emit("engine.notice", {})            # seq 2
        _publish(bus, "waiting")                 # seq 3
        _publish(bus, "running")                 # seq 4, evicts seq 1
        assert bus.since(1)[1] is False
        assert bus.since(0)[1] is True
        assert [e.seq for e in bus.since(2)[0]] == [3, 4]
