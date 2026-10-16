from __future__ import annotations

from textual import events
from textual.timer import Timer
from textual.widgets import Log

from lazyagent.engine.events import JOB_FINISHED, JOB_OUTPUT, JOB_STARTED, EngineEvent

# How long a finished job's output stays on screen. A failure is kept longer:
# its output is what explains it.
HIDE_AFTER_SUCCESS = 5.0
HIDE_AFTER_FAILURE = 30.0


class JobLog(Log):
    """Live output of the engine's jobs (custom worktree create/remove).

    Hidden until a job starts; shows the latest job only and hides itself a
    while after it finishes. Clicking it hides it at once.
    """

    DEFAULT_CSS = """
    JobLog {
        height: 10;
        border: solid $secondary;
        border-title-color: $text-muted;
        display: none;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(id="job-log", **kwargs)
        self.job_id: str | None = None
        self._title = ""
        self._hide_timer: Timer | None = None

    def on_job_event(self, event: EngineEvent) -> None:
        data = event.data
        if event.event == JOB_STARTED:
            self._cancel_hide()
            self.clear()
            self.job_id = data["job_id"]
            self._title = data.get("title") or "Job"
            self.border_title = f"{self._title} — running"
            self.write(f"$ {data.get('command', '')}\n")
            self.display = True
        elif data.get("job_id") != self.job_id:
            return
        elif event.event == JOB_OUTPUT:
            self.write(data["text"])
        elif event.event == JOB_FINISHED:
            code = data["exit_code"]
            ok = code == 0
            if ok:
                outcome = "done"
            elif code is None:
                outcome = "killed"
            else:
                outcome = f"failed (exit {code})"
            self.border_title = f"{self._title} — {outcome}"
            self._hide_timer = self.set_timer(
                HIDE_AFTER_SUCCESS if ok else HIDE_AFTER_FAILURE, self.hide
            )

    def hide(self) -> None:
        self._cancel_hide()
        self.display = False

    def on_click(self, event: events.Click) -> None:
        self.hide()

    def _cancel_hide(self) -> None:
        if self._hide_timer is not None:
            self._hide_timer.stop()
            self._hide_timer = None
