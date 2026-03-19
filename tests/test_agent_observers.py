from __future__ import annotations

import json

from lazyagent.agent_observers import (
    AgentLifecycleEvent,
    AgentObserver,
    ClaudeHooksObserver,
    CodexAppServerObserver,
    CompositeObserver,
    GeminiTelemetryObserver,
    GeminiPromptObserver,
    LifecycleConfidence,
)
from lazyagent.models import AgentStatus


class TestCompositeObserver:
    def test_flattens_events_from_multiple_observers(self):
        class FakeObserver(AgentObserver):
            def __init__(self, event):
                self._event = event

            def on_process_started(self):
                return [self._event]

        evt_a = AgentLifecycleEvent(
            status=AgentStatus.RUNNING,
            confidence=LifecycleConfidence.HIGH,
            detail="a",
        )
        evt_b = AgentLifecycleEvent(
            status=AgentStatus.WAITING,
            confidence=LifecycleConfidence.MEDIUM,
            detail="b",
        )
        composite = CompositeObserver([FakeObserver(evt_a), FakeObserver(evt_b)])
        events = composite.on_process_started()
        assert len(events) == 2
        assert events[0] is evt_a
        assert events[1] is evt_b

    def test_cleanup_calls_all_observers(self):
        cleaned = []

        class TrackingObserver(AgentObserver):
            def __init__(self, name):
                self._name = name

            def cleanup(self):
                cleaned.append(self._name)

        composite = CompositeObserver([TrackingObserver("a"), TrackingObserver("b")])
        composite.cleanup()
        assert cleaned == ["a", "b"]


class TestGeminiPromptObserver:
    def test_prompt_visible_sets_waiting(self):
        observer = GeminiPromptObserver()
        events = observer.on_screen_update(
            "some output\n\ngemini > ",
            current_status=AgentStatus.RUNNING,
            current_detail="",
        )
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING
        assert events[0].detail == "gemini prompt detected on screen"

    def test_prompt_missing_resumes_running(self):
        observer = GeminiPromptObserver()
        events = observer.on_screen_update(
            "some output\n\nrunning command...",
            current_status=AgentStatus.WAITING,
            current_detail="gemini prompt detected on screen",
        )
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING

    def test_no_transition_if_detail_mismatch(self):
        observer = GeminiPromptObserver()
        # If someone else set WAITING, we don't force it back to RUNNING
        events = observer.on_screen_update(
            "some output\n\nno prompt here",
            current_status=AgentStatus.WAITING,
            current_detail="sentinel visible on rendered screen",
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
        assert events[0].detail == "idle"

    def test_elicitation_sets_waiting_for_user(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps(
                {
                    "hook_event_name": "Notification",
                    "notification_type": "elicitation_dialog",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.WAITING_FOR_USER
        assert events[0].detail == "asking a question"

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

    def test_session_end_sets_completed(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps({"hook_event_name": "SessionEnd"}) + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.COMPLETED
        assert events[0].confidence == LifecycleConfidence.HIGH
        assert events[0].detail == "session ended"

    def test_pre_tool_use_sets_running(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash"}
            )
            + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING
        assert events[0].confidence == LifecycleConfidence.HIGH
        assert events[0].detail == "using Bash"

    def test_post_tool_use_sets_running(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps(
                {"hook_event_name": "PostToolUse", "tool_name": "Read"}
            )
            + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING
        assert events[0].confidence == LifecycleConfidence.HIGH
        assert events[0].detail == "Read done"

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
