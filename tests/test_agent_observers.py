from __future__ import annotations

import json

import pytest

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
from lazyagent.engine.manifest import INSTRUCTION_LIMIT
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

    def test_idle_prompt_sets_waiting(self, tmp_path):
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
        assert events[0].status == AgentStatus.WAITING
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

    def test_user_prompt_submit_sets_running(self, tmp_path):
        """Every turn starts RUNNING, including turns that use no tools."""
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hi"}) + "\n",
            encoding="utf-8",
        )
        observer = ClaudeHooksObserver(str(log_path))
        events = observer.poll()
        assert len(events) == 1
        assert events[0].status == AgentStatus.RUNNING
        assert events[0].confidence == LifecycleConfidence.HIGH
        assert events[0].detail == "prompt submitted"

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


class TestClaudeSubagentAfterStop:
    """Subagent tool calls after the main agent's Stop don't mean it is busy."""

    @staticmethod
    def _poll(tmp_path, *hooks: dict):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            "".join(json.dumps(h) + "\n" for h in hooks), encoding="utf-8"
        )
        return [e.status for e in ClaudeHooksObserver(str(log_path)).poll()]

    def test_ignored_after_stop(self, tmp_path):
        # The sequence seen once after a turn a notify_when_idle subscriber
        # was waiting on: Stop, then a lone subagent ReportFindings.
        statuses = self._poll(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "Stop"},
            {"hook_event_name": "PreToolUse", "tool_name": "ReportFindings",
             "agent_id": "aa3498b9fbed1bdae"},
        )
        assert statuses == [AgentStatus.RUNNING, AgentStatus.WAITING]

    def test_kept_during_a_turn(self, tmp_path):
        statuses = self._poll(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "PreToolUse", "tool_name": "Read", "agent_id": "sub1"},
        )
        assert statuses == [AgentStatus.RUNNING, AgentStatus.RUNNING]

    def test_kept_again_after_the_next_prompt(self, tmp_path):
        statuses = self._poll(
            tmp_path,
            {"hook_event_name": "Stop"},
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "PreToolUse", "tool_name": "Read", "agent_id": "sub1"},
        )
        assert statuses == [
            AgentStatus.WAITING, AgentStatus.RUNNING, AgentStatus.RUNNING
        ]

    def test_main_agent_tool_after_stop_still_counts(self, tmp_path):
        statuses = self._poll(
            tmp_path,
            {"hook_event_name": "Stop"},
            {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        )
        assert statuses == [AgentStatus.WAITING, AgentStatus.RUNNING]


class TestClaudeSessionStart:
    """At its prompt before any turn: a restored agent is idle, not running."""

    @staticmethod
    def _poll(tmp_path, *hooks: dict):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(
            "".join(json.dumps(h) + "\n" for h in hooks), encoding="utf-8"
        )
        return ClaudeHooksObserver(str(log_path)).poll()

    @pytest.mark.parametrize("source", ["resume", "startup", "clear"])
    def test_waiting(self, tmp_path, source):
        events = self._poll(tmp_path, {"hook_event_name": "SessionStart", "source": source})
        assert [(e.status, e.detail) for e in events] == [(AgentStatus.WAITING, "at its prompt")]
        assert events[0].confidence == LifecycleConfidence.HIGH

    def test_a_compaction_is_mid_turn(self, tmp_path):
        assert self._poll(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "SessionStart", "source": "compact"},
        )[-1].status == AgentStatus.RUNNING

    def test_a_first_prompt_runs_after_it(self, tmp_path):
        events = self._poll(
            tmp_path,
            {"hook_event_name": "SessionStart", "source": "startup"},
            {"hook_event_name": "UserPromptSubmit", "prompt": "Build it"},
        )
        assert [e.status for e in events] == [AgentStatus.WAITING, AgentStatus.RUNNING]

    def test_a_subagent_lone_tool_after_it_is_ignored(self, tmp_path):
        events = self._poll(
            tmp_path,
            {"hook_event_name": "SessionStart", "source": "resume"},
            {"hook_event_name": "PreToolUse", "tool_name": "Read", "agent_id": "sub1"},
        )
        assert [e.status for e in events] == [AgentStatus.WAITING]


class TestClaudeFirstPrompt:
    """The first main-agent prompt: the notes' pinned instruction (N3)."""

    @staticmethod
    def _observer(tmp_path, *hooks: dict) -> ClaudeHooksObserver:
        log_path = tmp_path / "hooks.jsonl"
        observer = ClaudeHooksObserver(str(log_path))
        with log_path.open("a", encoding="utf-8") as handle:
            for hook in hooks:
                handle.write(json.dumps(hook) + "\n")
        observer.poll()
        return observer

    def test_from_the_first_user_prompt_submit(self, tmp_path):
        observer = self._observer(
            tmp_path,
            {"hook_event_name": "SessionStart", "source": "startup"},
            {"hook_event_name": "UserPromptSubmit", "prompt": "Build the importer"},
        )
        assert observer.first_prompt == "Build the importer"

    def test_not_from_a_subagent(self, tmp_path):
        observer = self._observer(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit", "prompt": "sub", "agent_id": "s1"},
        )
        assert observer.first_prompt is None

    def test_not_overwritten_by_later_prompts(self, tmp_path):
        log_path = tmp_path / "hooks.jsonl"
        observer = self._observer(
            tmp_path, {"hook_event_name": "UserPromptSubmit", "prompt": "first"}
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "second"})
                + "\n"
            )
        observer.poll()
        assert observer.first_prompt == "first"

    def test_blank_or_missing_prompts_are_skipped(self, tmp_path):
        observer = self._observer(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit"},
            {"hook_event_name": "UserPromptSubmit", "prompt": "  "},
            {"hook_event_name": "UserPromptSubmit", "prompt": "real one"},
        )
        assert observer.first_prompt == "real one"

    def test_clipped(self, tmp_path):
        observer = self._observer(
            tmp_path,
            {"hook_event_name": "UserPromptSubmit", "prompt": "x" * (INSTRUCTION_LIMIT + 9)},
        )
        assert observer.first_prompt == "x" * INSTRUCTION_LIMIT

    def test_session_start_sets_the_conversation_id(self, tmp_path):
        observer = self._observer(
            tmp_path,
            {"hook_event_name": "SessionStart", "source": "startup", "session_id": "c1"},
        )
        assert observer.conversation_id == "c1"
        assert observer.first_prompt is None

    def test_a_composite_forwards_it(self, tmp_path):
        observer = self._observer(
            tmp_path, {"hook_event_name": "UserPromptSubmit", "prompt": "first"}
        )
        assert CompositeObserver([AgentObserver(), observer]).first_prompt == "first"
        assert CompositeObserver([AgentObserver()]).first_prompt is None


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


class TestJsonlPollIsCheap:
    def test_unchanged_log_is_not_reopened(self, tmp_path, monkeypatch):
        log_path = tmp_path / "hooks.jsonl"
        log_path.write_text(json.dumps({"hook_event_name": "Stop"}) + "\n")
        observer = ClaudeHooksObserver(str(log_path))
        assert len(observer.poll()) == 1

        opened = []
        real_open = type(log_path).open
        monkeypatch.setattr(
            type(log_path), "open", lambda self, *a, **k: opened.append(1) or real_open(self, *a, **k)
        )
        assert observer.poll() == []
        assert opened == []

        with log_path.open("a") as handle:
            handle.write(json.dumps({"hook_event_name": "UserPromptSubmit"}) + "\n")
        assert [e.detail for e in observer.poll()] == ["prompt submitted"]


def test_only_screen_reading_observers_need_the_screen(tmp_path):
    claude = ClaudeHooksObserver(str(tmp_path / "hooks.jsonl"))
    assert AgentObserver().needs_screen is False
    assert claude.needs_screen is False
    assert GeminiPromptObserver().needs_screen is True
    assert CompositeObserver([claude]).needs_screen is False
    assert CompositeObserver([claude, GeminiPromptObserver()]).needs_screen is True


def test_a_composite_reports_its_childs_conversation_id(tmp_path):
    claude = ClaudeHooksObserver(str(tmp_path / "hooks.jsonl"))
    composite = CompositeObserver([AgentObserver(), claude])
    assert composite.conversation_id is None
    claude.conversation_id = "c-1"
    assert composite.conversation_id == "c-1"
