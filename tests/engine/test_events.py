"""EventBus replay: the history after a cursor, and whether it has a gap."""

from __future__ import annotations

from lazyagent.engine.events import EventBus


def test_replay_returns_the_history_after_the_cursor():
    bus = EventBus()
    for i in range(5):
        bus.emit("x.event", {"i": i})
    events, truncated = bus.replay(2)
    assert [e.seq for e in events] == [3, 4, 5]
    assert truncated is False
    assert bus.replay(5) == ([], False)


def test_replay_reports_evicted_events():
    bus = EventBus(log_maxlen=3)
    for i in range(6):
        bus.emit("x.event", {"i": i})
    events, truncated = bus.replay(1)
    assert [e.seq for e in events] == [4, 5, 6]
    assert truncated is True
    assert bus.replay(3) == (events, False)
