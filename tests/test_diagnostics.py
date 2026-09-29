"""Tests for the opt-in diagnostic logging."""

from __future__ import annotations

import json
import logging

import pytest

from lazyagent import diagnostics as diag


@pytest.fixture
def diag_env(monkeypatch, tmp_path):
    """Reset the module's global state around each test and restore it after.

    ``setup()`` is process-global by design (one log file per run), so the
    globals and the ``lazyagent`` logger have to be snapshotted here or one
    test would configure logging for the whole session.
    """
    root = logging.getLogger("lazyagent")
    saved = (
        diag.ENABLED,
        diag.HOT,
        diag._configured,
        diag._log_path,
        list(root.handlers),
        root.level,
        root.propagate,
    )
    for var in (
        "LAZYAGENT_LOG",
        "LAZYAGENT_LOG_LEVEL",
        "LAZYAGENT_LOG_FILE",
        "LAZYAGENT_LOG_WATCHDOG_MS",
    ):
        monkeypatch.delenv(var, raising=False)
    diag.ENABLED = False
    diag.HOT = False
    diag._configured = False
    diag._log_path = None
    root.handlers = []

    yield tmp_path / "lz.log"

    for handler in root.handlers:
        handler.close()
    (
        diag.ENABLED,
        diag.HOT,
        diag._configured,
        diag._log_path,
        root.handlers,
        root.level,
        root.propagate,
    ) = saved


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestEnablement:
    def test_disabled_by_default(self, diag_env):
        assert diag.setup() is None
        assert diag.ENABLED is False
        assert diag.HOT is False
        assert not diag_env.exists()

    def test_enabled_by_flag(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG", "1")
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        assert diag.setup() == diag_env
        assert diag.ENABLED is True
        # INFO by default: the hot paths stay off until asked for.
        assert diag.HOT is False

    def test_explicit_off_wins(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG", "0")
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        assert diag.setup() is None
        assert diag.ENABLED is False

    def test_debug_level_enables_hot_paths(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_LEVEL", "debug")
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        assert diag.HOT is True

    def test_log_file_alone_enables(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        assert diag.setup() == diag_env

    def test_setup_is_idempotent(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        first = diag.setup()
        assert diag.setup() is first
        assert len(logging.getLogger("lazyagent").handlers) == 1

    def test_unwritable_path_disables_rather_than_raises(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", "/proc/nope/lazyagent.log")
        assert diag.setup() is None
        assert diag.ENABLED is False

    def test_default_path_is_xdg_state_home(self, diag_env, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("LAZYAGENT_LOG", "1")
        assert diag.setup() == tmp_path / "state" / "lazyagent" / "lazyagent.log"


class TestEmitting:
    def test_event_is_a_json_line(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        diag.event(diag.log, "test.event", worktree="/tmp/wt", count=3)

        record = _records(diag_env)[-1]
        assert record["event"] == "test.event"
        assert record["worktree"] == "/tmp/wt"
        assert record["count"] == 3
        assert record["level"] == "INFO"
        assert record["thread"] == "MainThread"
        assert record["ts"].count(".") == 1  # millisecond precision

    def test_event_is_silent_when_disabled(self, diag_env):
        diag.setup()
        diag.event(diag.log, "test.event")
        assert not diag_env.exists()

    def test_debug_event_needs_hot(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()  # INFO — not hot
        diag.debug_event(diag.log, "hot.event")
        assert not any(r["event"] == "hot.event" for r in _records(diag_env))

    def test_timed_reports_duration_and_extras(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        with diag.timed(diag.log, "test.block", worktree="/tmp/wt") as span:
            span["bytes"] = 12

        record = _records(diag_env)[-1]
        assert record["event"] == "test.block"
        assert record["worktree"] == "/tmp/wt"
        assert record["bytes"] == 12
        assert record["duration_ms"] >= 0

    def test_timed_still_reports_when_the_block_raises(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        with pytest.raises(ValueError):
            with diag.timed(diag.log, "test.boom"):
                raise ValueError("boom")

        record = _records(diag_env)[-1]
        assert record["event"] == "test.boom"
        assert record["failed"] is True

    def test_timed_yields_a_dict_when_disabled(self, diag_env):
        diag.setup()
        with diag.timed(diag.log, "test.block") as span:
            span["ignored"] = True
        assert not diag_env.exists()

    def test_plain_logging_calls_are_captured(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        logging.getLogger("lazyagent.somewhere").warning("hello %s", "world")

        record = _records(diag_env)[-1]
        assert record["event"] == "message"
        assert record["msg"] == "hello world"
        assert record["logger"] == "lazyagent.somewhere"


class TestWrapTimer:
    def test_returns_the_callback_untouched_when_disabled(self, diag_env):
        diag.setup()

        def callback():
            return "value"

        assert diag.wrap_timer("timer.test", callback) is callback

    def test_traces_entry_and_exit(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        wrapped = diag.wrap_timer("timer.test", lambda: "value")

        assert wrapped() == "value"

        events = [r["event"] for r in _records(diag_env)]
        assert "timer.test.enter" in events
        assert "timer.test.exit" in events


class TestGauge:
    def test_counts_concurrent_occupants(self, diag_env, monkeypatch):
        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        diag.setup()
        gauge = diag.Gauge()

        with gauge as outer:
            with gauge as inner:
                assert (outer, inner) == (1, 2)
        assert gauge.peak == 2

        with gauge as again:
            assert again == 1

    def test_is_inert_when_disabled(self, diag_env):
        diag.setup()
        gauge = diag.Gauge()
        with gauge as n:
            assert n == 0
        assert gauge.peak == 0


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_reports_a_blocked_loop_with_a_stack(self, diag_env, monkeypatch):
        import asyncio
        import time

        monkeypatch.setenv("LAZYAGENT_LOG_FILE", str(diag_env))
        monkeypatch.setenv("LAZYAGENT_LOG_WATCHDOG_MS", "50")
        diag.setup()
        diag.start_watchdog()
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.3)  # block the loop on purpose
            await asyncio.sleep(0.2)
        finally:
            diag.stop_watchdog()

        blocked = [r for r in _records(diag_env) if r["event"] == "loop.blocked"]
        assert blocked, "watchdog did not notice a 300 ms block"
        assert blocked[0]["blocked_ms"] >= 50
        assert any("test_diagnostics.py" in frame for frame in blocked[0]["stack"])

    def test_start_is_a_no_op_when_disabled(self, diag_env):
        diag.setup()
        diag.start_watchdog()  # no running loop; must not raise
        assert diag._watchdog is None
