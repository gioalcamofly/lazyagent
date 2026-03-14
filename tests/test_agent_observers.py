from __future__ import annotations

import json

from lazyagent.agent_observers import (
    ClaudeHooksObserver,
    CodexAppServerObserver,
    GeminiTelemetryObserver,
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
    def test_notification_event_sets_waiting_for_approval(self, tmp_path):
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
        assert events[0].status == AgentStatus.WAITING_FOR_APPROVAL
        assert events[0].confidence == LifecycleConfidence.HIGH

    def test_idle_prompt_sets_waiting_for_user(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "hook_event_name": "Notification",
                    "notification_type": "idle_prompt",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING_FOR_USER

    def test_stop_event_sets_completed(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps({"hook_event_name": "Stop"}) + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.COMPLETED

    def test_poll_is_incremental(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text("", encoding="utf-8")
        observer = ClaudeHooksObserver(str(log_path))
        assert observer.poll() == []
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"hook_event_name": "TaskCompleted"}) + "\n")
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.COMPLETED
        assert observer.poll() == []


class TestCodexAppServerObserver:
    def test_turn_started_sets_running(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text(
            json.dumps({"method": "turn/started"}) + "\n",
            encoding="utf-8",
        )
        observer = CodexAppServerObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING
        assert events[0].confidence == LifecycleConfidence.HIGH

    def test_waiting_on_approval_sets_approving(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "method": "thread/status/changed",
                    "params": {"status": "waitingOnApproval"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observer = CodexAppServerObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING_FOR_APPROVAL

    def test_turn_completed_sets_completed(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text(
            json.dumps({"method": "turn/completed"}) + "\n",
            encoding="utf-8",
        )
        observer = CodexAppServerObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.COMPLETED

    def test_turn_failed_sets_failed(self, tmp_path):
        log_path = tmp_path / "events.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "method": "turn/failed",
                    "params": {"error": "tool timeout"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observer = CodexAppServerObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.FAILED
        assert "tool timeout" in events[0].detail


class TestGeminiTelemetryObserver:
    def test_activity_event_sets_running(self, tmp_path):
        log_path = tmp_path / "telemetry.jsonl"
        log_path.write_text(
            json.dumps({"event_type": "tool_call"}) + "\n",
            encoding="utf-8",
        )
        observer = GeminiTelemetryObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING
        assert events[0].confidence == LifecycleConfidence.MEDIUM

    def test_error_event_sets_failed(self, tmp_path):
        log_path = tmp_path / "telemetry.jsonl"
        log_path.write_text(
            json.dumps({"event_type": "error", "message": "auth failed"}) + "\n",
            encoding="utf-8",
        )
        observer = GeminiTelemetryObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.FAILED
        assert "auth failed" in events[0].detail

    def test_session_end_sets_completed(self, tmp_path):
        log_path = tmp_path / "telemetry.jsonl"
        log_path.write_text(
            json.dumps({"event_type": "session_end"}) + "\n",
            encoding="utf-8",
        )
        observer = GeminiTelemetryObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.COMPLETED
