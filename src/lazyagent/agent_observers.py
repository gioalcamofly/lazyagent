from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lazyagent.models import AgentStatus, LifecycleConfidence


@dataclass(frozen=True)
class AgentLifecycleEvent:
    """Normalized lifecycle update produced by a provider observer."""

    status: AgentStatus
    confidence: LifecycleConfidence
    detail: str = ""


class AgentObserver:
    """Base interface for provider-specific lifecycle observers."""

    def on_process_started(self) -> list[AgentLifecycleEvent]:
        return []

    def on_terminal_output(self, chars: str) -> list[AgentLifecycleEvent]:
        return []

    def on_screen_update(
        self,
        screen_text: str,
        *,
        current_status: AgentStatus,
        current_detail: str = "",
    ) -> list[AgentLifecycleEvent]:
        return []

    def on_disconnect(self) -> list[AgentLifecycleEvent]:
        return []

    def poll(self) -> list[AgentLifecycleEvent]:
        return []

    def cleanup(self) -> None:
        return None


class CompositeObserver(AgentObserver):
    """Observer that combines multiple observation strategies."""

    def __init__(self, observers: list[AgentObserver]) -> None:
        self._observers = observers

    def on_process_started(self) -> list[AgentLifecycleEvent]:
        return _flatten(observer.on_process_started() for observer in self._observers)

    def on_terminal_output(self, chars: str) -> list[AgentLifecycleEvent]:
        return _flatten(
            observer.on_terminal_output(chars) for observer in self._observers
        )

    def on_screen_update(
        self,
        screen_text: str,
        *,
        current_status: AgentStatus,
        current_detail: str = "",
    ) -> list[AgentLifecycleEvent]:
        return _flatten(
            observer.on_screen_update(
                screen_text,
                current_status=current_status,
                current_detail=current_detail,
            )
            for observer in self._observers
        )

    def on_disconnect(self) -> list[AgentLifecycleEvent]:
        return _flatten(observer.on_disconnect() for observer in self._observers)

    def poll(self) -> list[AgentLifecycleEvent]:
        return _flatten(observer.poll() for observer in self._observers)

    def cleanup(self) -> None:
        for observer in self._observers:
            observer.cleanup()


class TerminalSentinelObserver(AgentObserver):
    """Fallback observer that infers state from rendered terminal contents."""

    def __init__(self, sentinel_text: str) -> None:
        self._sentinel_text = sentinel_text.lower()

    def on_screen_update(
        self,
        screen_text: str,
        *,
        current_status: AgentStatus,
        current_detail: str = "",
    ) -> list[AgentLifecycleEvent]:
        rendered = screen_text.lower()
        if self._sentinel_text in rendered:
            return [
                AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.LOW,
                    detail="sentinel visible on rendered screen",
                )
            ]
        # Only revert to RUNNING if we were the one who set WAITING via sentinel
        if current_status in (AgentStatus.WAITING, AgentStatus.POSSIBLY_HANGED) and \
           current_detail == "sentinel visible on rendered screen":
            return [
                AgentLifecycleEvent(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.LOW,
                    detail="sentinel no longer visible on rendered screen",
                )
            ]
        return []


class ClaudeHooksObserver(AgentObserver):
    """Observer that tails Claude hook events written to a JSONL file."""

    def __init__(self, log_path: str, temp_dir: str | None = None) -> None:
        self._log_path = Path(log_path)
        self._temp_dir = Path(temp_dir) if temp_dir else None
        self._offset = 0

    def poll(self) -> list[AgentLifecycleEvent]:
        if not self._log_path.exists():
            return []

        events: list[AgentLifecycleEvent] = []
        with self._log_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                payload = line.strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                mapped = self._map_hook_event(data)
                if mapped is not None:
                    events.append(mapped)
            self._offset = handle.tell()
        return events

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _map_hook_event(self, data: dict) -> AgentLifecycleEvent | None:
        event_name = data.get("hook_event_name")
        if event_name == "Notification":
            notification_type = str(data.get("notification_type", "")).strip().lower()
            if notification_type == "permission_prompt":
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_APPROVAL,
                    confidence=LifecycleConfidence.HIGH,
                    detail="claude permission prompt",
                )
            if notification_type in {"idle_prompt", "elicitation_dialog"}:
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_USER,
                    confidence=LifecycleConfidence.HIGH,
                    detail=f"claude {notification_type}",
                )
            return None
        if event_name == "Stop":
            return AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
                detail="claude stopped (waiting for input)",
            )
        if event_name == "TaskCompleted":
            return AgentLifecycleEvent(
                status=AgentStatus.COMPLETED,
                confidence=LifecycleConfidence.HIGH,
                detail="claude task completed",
            )
        return None


class CodexAppServerObserver(AgentObserver):
    """Observer that consumes Codex App Server events from a JSONL file."""

    def __init__(self, log_path: str, temp_dir: str | None = None) -> None:
        self._log_path = Path(log_path)
        self._temp_dir = Path(temp_dir) if temp_dir else None
        self._offset = 0

    def poll(self) -> list[AgentLifecycleEvent]:
        if not self._log_path.exists():
            return []

        events: list[AgentLifecycleEvent] = []
        with self._log_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                payload = line.strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                mapped = self._map_app_server_event(data)
                if mapped is not None:
                    events.append(mapped)
            self._offset = handle.tell()
        return events

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _map_app_server_event(self, data: dict) -> AgentLifecycleEvent | None:
        # Based on documented Codex App Server event shapes
        method = data.get("method")
        params = data.get("params", {})

        if method == "turn/started":
            return AgentLifecycleEvent(
                status=AgentStatus.RUNNING,
                confidence=LifecycleConfidence.HIGH,
                detail="codex turn started",
            )
        if method == "turn/completed":
            return AgentLifecycleEvent(
                status=AgentStatus.COMPLETED,
                confidence=LifecycleConfidence.HIGH,
                detail="codex turn completed",
            )
        if method == "turn/failed":
            return AgentLifecycleEvent(
                status=AgentStatus.FAILED,
                confidence=LifecycleConfidence.HIGH,
                detail=f"codex turn failed: {params.get('error', 'unknown error')}",
            )
        if method == "thread/status/changed":
            status = params.get("status")
            if isinstance(status, dict):
                active_flags = status.get("activeFlags", [])
                if "waitingOnApproval" in active_flags:
                    return AgentLifecycleEvent(
                        status=AgentStatus.WAITING_FOR_APPROVAL,
                        confidence=LifecycleConfidence.HIGH,
                        detail="codex waiting on approval",
                    )
                if "waitingOnUser" in active_flags:
                    return AgentLifecycleEvent(
                        status=AgentStatus.WAITING_FOR_USER,
                        confidence=LifecycleConfidence.HIGH,
                        detail="codex waiting on user",
                    )
            elif status == "waitingOnApproval":
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_APPROVAL,
                    confidence=LifecycleConfidence.HIGH,
                    detail="codex waiting on approval",
                )
            elif status == "waitingOnUser":
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_USER,
                    confidence=LifecycleConfidence.HIGH,
                    detail="codex waiting on user",
                )
        return None


class GeminiTelemetryObserver(AgentObserver):
    """Observer that interprets Gemini telemetry streams from a JSONL file."""

    def __init__(self, log_path: str, temp_dir: str | None = None) -> None:
        self._log_path = Path(log_path)
        self._temp_dir = Path(temp_dir) if temp_dir else None
        self._offset = 0

    def poll(self) -> list[AgentLifecycleEvent]:
        if not self._log_path.exists():
            return []

        events: list[AgentLifecycleEvent] = []
        with self._log_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                payload = line.strip()
                if not payload:
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                mapped = self._map_telemetry_event(data)
                if mapped is not None:
                    events.append(mapped)
            self._offset = handle.tell()
        return events

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _map_telemetry_event(self, data: dict) -> AgentLifecycleEvent | None:
        event_type = data.get("event_type")

        # Telemetry provides activity signals (with gemini_cli. prefix)
        if event_type in {
            "gemini_cli.api_request",
            "gemini_cli.tool_call",
            "gemini_cli.file.operation",
            "request",  # keep old values as fallback
            "tool_call",
            "file_operation",
        }:
            return AgentLifecycleEvent(
                status=AgentStatus.RUNNING,
                confidence=LifecycleConfidence.MEDIUM,
                detail=f"gemini activity: {event_type}",
            )
        if event_type in {
            "gemini_cli.turn_end",
            "gemini_cli.waiting_for_input",
            "gemini_cli.prompt",
            "gemini_cli.api_response",  # After response, we usually wait
        }:
            return AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
                detail=f"gemini {event_type}",
            )
        if event_type in {"gemini_cli.error", "error"}:
            return AgentLifecycleEvent(
                status=AgentStatus.FAILED,
                confidence=LifecycleConfidence.HIGH,
                detail=f"gemini error: {data.get('message', 'unknown')}",
            )
        if event_type in {"gemini_cli.session", "session_end", "gemini_cli.completion"}:
            # session event usually marks lifecycle boundaries
            return AgentLifecycleEvent(
                status=AgentStatus.COMPLETED,
                confidence=LifecycleConfidence.MEDIUM,
                detail="gemini session update",
            )
        return None


class GeminiPromptObserver(AgentObserver):
    """Observer that detects the Gemini CLI prompt (e.g., 'gemini >') on the screen."""

    def on_screen_update(
        self,
        screen_text: str,
        *,
        current_status: AgentStatus,
        current_detail: str = "",
    ) -> list[AgentLifecycleEvent]:
        # Typical gemini prompts end with "> " or "gemini > "
        # We check if any of the last few lines (non-empty) matches this
        lines = [line.strip() for line in screen_text.splitlines() if line.strip()]
        if not lines:
            return []

        last_line = lines[-1]
        # Match "gemini >", "ai >", or just ">" at the end of the line
        if last_line.endswith(">") or " > " in last_line:
            if current_status == AgentStatus.RUNNING:
                return [
                    AgentLifecycleEvent(
                        status=AgentStatus.WAITING,
                        confidence=LifecycleConfidence.MEDIUM,
                        detail="gemini prompt detected on screen",
                    )
                ]
        elif current_status == AgentStatus.WAITING and \
             current_detail == "gemini prompt detected on screen":
            return [
                AgentLifecycleEvent(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.LOW,
                    detail="gemini prompt no longer visible",
                )
            ]
        return []


def _flatten(batches) -> list[AgentLifecycleEvent]:
    events: list[AgentLifecycleEvent] = []
    for batch in batches:
        events.extend(batch)
    return events
