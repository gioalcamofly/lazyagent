from __future__ import annotations

import json

from lazyagent.agent_observers import (
    ClaudeHooksObserver,
    LifecycleConfidence,
    TerminalSentinelObserver,
)
from lazyagent.models import AgentStatus


class TestTerminalSentinelObserver:
    def test_sentinel_visible_sets_waiting(self):
        observer = TerminalSentinelObserver("your turn")
        events = observer.on_screen_update(
            "line 1\nyour turn\n",
            current_status=AgentStatus.RUNNING,
        )
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING
        assert events[0].confidence == LifecycleConfidence.LOW

    def test_sentinel_missing_resumes_running(self):
        observer = TerminalSentinelObserver("your turn")
        events = observer.on_screen_update(
            "line 1\nstill working\n",
            current_status=AgentStatus.WAITING,
        )
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING

    def test_no_status_change_when_running_without_sentinel(self):
        observer = TerminalSentinelObserver("your turn")
        events = observer.on_screen_update(
            "line 1\nstill working\n",
            current_status=AgentStatus.RUNNING,
        )
        assert events == []


class TestClaudeHooksObserver:
    def test_notification_event_sets_waiting(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "hook_event_name": "Notification",
                    "notification_type": "permission_prompt",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING
        assert events[0].confidence == LifecycleConfidence.HIGH

    def test_stop_event_sets_waiting(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps({"hook_event_name": "Stop"}) + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING

    def test_poll_is_incremental(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text("", encoding="utf-8")
        observer = ClaudeHooksObserver(str(log_path))
        assert observer.poll() == []
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"hook_event_name": "TaskCompleted"}) + "\n")
        events = observer.poll()
        assert len(events) == 1
        assert observer.poll() == []
