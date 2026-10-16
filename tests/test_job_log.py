"""JobLog: the engine's job output on screen."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from lazyagent.engine.events import JOB_FINISHED, JOB_OUTPUT, JOB_STARTED, EngineEvent
from lazyagent.widgets import job_log
from lazyagent.widgets.job_log import JobLog


class _Host(App):
    def compose(self) -> ComposeResult:
        yield JobLog()


def _event(name, **data):
    return EngineEvent(0, name, data)


@pytest.mark.asyncio
async def test_shows_the_latest_job_and_hides_after_it_finishes(monkeypatch):
    monkeypatch.setattr(job_log, "HIDE_AFTER_SUCCESS", 0.05)
    app = _Host()
    async with app.run_test() as pilot:
        log = app.query_one(JobLog)
        assert not log.display

        log.on_job_event(_event(JOB_STARTED, job_id="j1", title="Create worktree feat", command="mk feat"))
        log.on_job_event(_event(JOB_OUTPUT, job_id="j1", text="step 1\nstep 2\n"))
        log.on_job_event(_event(JOB_OUTPUT, job_id="j0", text="stale\n"))
        await pilot.pause()

        assert log.display
        assert log.border_title == "Create worktree feat — running"
        assert list(log.lines)[:3] == ["$ mk feat", "step 1", "step 2"]
        assert "stale" not in "\n".join(log.lines)

        log.on_job_event(_event(JOB_FINISHED, job_id="j1", exit_code=0))
        assert log.border_title == "Create worktree feat — done"
        await pilot.pause(0.2)
        assert not log.display


@pytest.mark.asyncio
async def test_a_failure_stays_up_with_its_exit_code():
    app = _Host()
    async with app.run_test() as pilot:
        log = app.query_one(JobLog)
        log.on_job_event(_event(JOB_STARTED, job_id="j1", title="Remove worktree x", command="rm"))
        log.on_job_event(_event(JOB_FINISHED, job_id="j1", exit_code=3))
        await pilot.pause(0.1)

        assert log.display
        assert log.border_title == "Remove worktree x — failed (exit 3)"

        await pilot.click(JobLog)
        assert not log.display
