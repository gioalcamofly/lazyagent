from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from lazyagent.engine.manifest import clip_instruction
from lazyagent.engine.questions import last_assistant_text, turn_question
from lazyagent.models import AgentStatus, LifecycleConfidence

# How long a turn's "background work is running" holds with no wake-up: the
# hook only tells at the end of a turn, so past this the agent reads as it
# would without the signal. As long as a status_report's default ball "agent".
BACKGROUND_STALE_SECONDS = 2 * 3600.0


@dataclass(frozen=True)
class AgentLifecycleEvent:
    """Normalized lifecycle update produced by a provider observer."""

    status: AgentStatus
    confidence: LifecycleConfidence
    detail: str = ""
    # The question the finished turn ends with (an idle status only): the
    # agent asked in prose and waits (engine/questions.py).
    question: str = ""
    # Whether the agent has had a turn since its process started (see
    # AgentObserver.turned), as of this event.
    turned: bool = True
    # How many times it has run again after ending a turn with background
    # work running (see AgentObserver.background_runs), as of this event.
    background_runs: int = 0
    # Its turn ended with background work running (see
    # AgentObserver.waiting_on_background), as of this event.
    background: bool = False


class AgentObserver:
    """Base interface for provider-specific lifecycle observers."""

    #: The provider's id of the conversation the agent runs, once seen
    #: (Claude's ``session_id``); a restore resumes exactly that one.
    conversation_id: str | None = None

    #: The first prompt the agent got (typed, or sent by lazyagent), clipped:
    #: the notes' pinned instruction. None when the provider does not say.
    first_prompt: str | None = None

    #: Whether :meth:`on_screen_update` reads the screen. Those that get
    #: their status from hooks or logs do not, and the session then skips
    #: rendering the screen to text for them (proposal 0005).
    needs_screen: bool = False

    #: Whether the agent has had a turn (a prompt submitted, or a turn
    #: ended) since its process started. An agent at its prompt with none
    #: has finished nothing: a restore, or a spawn with no instruction. True
    #: for the providers that do not say, so they read as before.
    turned: bool = True

    #: Whether its turn ended with its own background work running, so it
    #: will be woken: quiet, but neither hanged nor finished.
    waiting_on_background: bool = False

    #: How many times it has run again after such a turn. A status report
    #: made before one no longer speaks for the turn that ends after it.
    background_runs: int = 0

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

    @property
    def needs_screen(self) -> bool:  # type: ignore[override]
        return any(observer.needs_screen for observer in self._observers)

    @property
    def conversation_id(self) -> str | None:  # type: ignore[override]
        return next(
            (o.conversation_id for o in self._observers if o.conversation_id), None
        )

    @property
    def first_prompt(self) -> str | None:  # type: ignore[override]
        return next((o.first_prompt for o in self._observers if o.first_prompt), None)

    @property
    def turned(self) -> bool:  # type: ignore[override]
        return all(observer.turned for observer in self._observers)

    @property
    def waiting_on_background(self) -> bool:  # type: ignore[override]
        return any(observer.waiting_on_background for observer in self._observers)

    @property
    def background_runs(self) -> int:  # type: ignore[override]
        return max((o.background_runs for o in self._observers), default=0)

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


class JsonlTailObserver(AgentObserver):
    """Base observer that incrementally tails a JSONL log file.

    Subclasses implement ``_map_event`` to convert each parsed JSON object
    into an optional ``AgentLifecycleEvent``.
    """

    def __init__(self, log_path: str, temp_dir: str | None = None) -> None:
        self._log_path = Path(log_path)
        self._temp_dir = Path(temp_dir) if temp_dir else None
        self._offset = 0

    def poll(self) -> list[AgentLifecycleEvent]:
        try:
            size = self._log_path.stat().st_size
        except FileNotFoundError:
            return []
        if size == self._offset:
            # Polled every second for every agent: don't open an unchanged log.
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
                mapped = self._map_event(data)
                if mapped is not None:
                    events.append(mapped)
            self._offset = handle.tell()
        return events

    def cleanup(self) -> None:
        if self._temp_dir is not None:
            shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _map_event(self, data: dict) -> AgentLifecycleEvent | None:
        raise NotImplementedError


class ClaudeHooksObserver(JsonlTailObserver):
    """Observer that tails Claude hook events written to a JSONL file."""

    def __init__(
        self,
        log_path: str,
        temp_dir: str | None = None,
        *,
        prompted: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(log_path, temp_dir=temp_dir)
        # False between the main agent's Stop and the next prompt.
        self._turn_open = True
        # The question the last finished turn ended with, until the next one.
        self._question = ""
        # Started with an instruction (the CLI's prompt argument): its
        # SessionStart comes before that prompt's UserPromptSubmit.
        self._prompted = prompted
        self.turned = False
        self._clock = clock
        # While its turn ended with background work running: since when and
        # what the row says.
        self._background_since: float | None = None
        self._background_detail = ""
        # An approval or a dialog is up meanwhile (a subagent's): it stays
        # the user's, whatever the staleness says.
        self._background_asking = False
        # This turn started by itself (a finished task, a monitor's event, a
        # cron), not from a prompt someone sent.
        self._self_woken = False
        self._cron_prompts: tuple[str, ...] = ()
        self.background_runs = 0

    @property
    def waiting_on_background(self) -> bool:  # type: ignore[override]
        return self._background_since is not None

    def poll(self) -> list[AgentLifecycleEvent]:
        events = super().poll()
        since = self._background_since
        if (
            since is not None
            and not self._background_asking
            and self._clock() - since >= BACKGROUND_STALE_SECONDS
        ):
            # No wake-up all this time: back to what a Stop says without the
            # signal, which the user can see and act on.
            self._background_since = None
            events.append(
                self._stamped(
                    AgentLifecycleEvent(
                        status=AgentStatus.WAITING,
                        confidence=LifecycleConfidence.HIGH,
                        detail="waiting for input",
                    )
                )
            )
        return events

    def on_disconnect(self) -> list[AgentLifecycleEvent]:
        self._background_since = None
        return super().on_disconnect()

    def _map_event(self, data: dict) -> AgentLifecycleEvent | None:
        event = self._map(data)
        return None if event is None else self._stamped(event)

    def _stamped(self, event: AgentLifecycleEvent) -> AgentLifecycleEvent:
        return replace(
            event,
            turned=self.turned,
            background_runs=self.background_runs,
            background=self.waiting_on_background,
        )

    def _ran_again(self) -> None:
        """The main agent acts: a wait on background work is over."""
        if self._background_since is not None:
            self._background_since = None
            self.background_runs += 1

    def _background_event(self) -> AgentLifecycleEvent:
        self._background_asking = False
        return AgentLifecycleEvent(
            status=AgentStatus.RUNNING,
            confidence=LifecycleConfidence.HIGH,
            detail=self._background_detail,
        )

    def _map(self, data: dict) -> AgentLifecycleEvent | None:

        event_name = data.get("hook_event_name")
        conversation = data.get("session_id")
        if isinstance(conversation, str) and conversation and not data.get("agent_id"):
            self.conversation_id = conversation
        if event_name in ("PreToolUse", "PostToolUse") and data.get("agent_id"):
            # A subagent's tool call after the main agent's Stop isn't the
            # agent working. Seen from Claude Code 2.1.295 after a turn a
            # SendMessage notify_when_idle subscriber was waiting on: a lone
            # PreToolUse with no PostToolUse or Stop, which left the agent
            # showing as running until the next idle_prompt notification.
            if not self._turn_open:
                return self._background_event() if self.waiting_on_background else None
        elif event_name in ("PreToolUse", "PostToolUse"):
            self._ran_again()
        if event_name == "Notification":
            notification_type = str(data.get("notification_type", "")).strip().lower()
            if notification_type == "permission_prompt":
                self._background_asking = self.waiting_on_background
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_APPROVAL,
                    confidence=LifecycleConfidence.HIGH,
                    detail="needs approval",
                )
            if notification_type == "idle_prompt":
                # Claude's "still waiting for your input" reminder for an agent
                # that finished its turn. It is not a dialog: reporting it as
                # waiting_for_user would put idle agents in the "blocked" group.
                if self.waiting_on_background:
                    return self._background_event()
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING,
                    confidence=LifecycleConfidence.HIGH,
                    detail="idle",
                    question=self._question,
                )
            if notification_type == "elicitation_dialog":
                self._background_asking = self.waiting_on_background
                return AgentLifecycleEvent(
                    status=AgentStatus.WAITING_FOR_USER,
                    confidence=LifecycleConfidence.HIGH,
                    detail="asking a question",
                )
            return None
        if event_name == "SessionStart" and not data.get("agent_id"):
            if data.get("source") == "compact":
                return None  # mid-turn: the turn goes on
            self._background_since = None
            if self._prompted and not self.turned:
                # Spawned with an instruction: its first prompt is on the
                # way, so it is not at its prompt, nor done with anything.
                return AgentLifecycleEvent(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.HIGH,
                    detail="starting",
                )
            # At its prompt before any turn: a restore (``--resume``) types
            # nothing, so without this its first output left it running
            # until the hang check called it possibly hanged.
            self._turn_open = False
            return AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
                detail="at its prompt",
            )
        if event_name == "UserPromptSubmit":
            prompt = data.get("prompt")
            if not data.get("agent_id"):
                self.turned = True
                self._ran_again()
                self._self_woken = _self_woken(prompt, self._cron_prompts)
            self._turn_open = True
            self._question = ""
            if (
                self.first_prompt is None
                and not data.get("agent_id")
                and isinstance(prompt, str)
                and prompt.strip()
            ):
                self.first_prompt = clip_instruction(prompt)
            # The start of every turn. Without it a turn that uses no tools
            # never leaves WAITING, so nothing can tell it happened — and it
            # is the direct confirmation that programmatic input was submitted.
            return AgentLifecycleEvent(
                status=AgentStatus.RUNNING,
                confidence=LifecycleConfidence.HIGH,
                detail="prompt submitted",
            )
        if event_name == "Stop":
            self.turned = True
            self._turn_open = False
            self._question = _stop_question(data)
            self._background_since = None
            self._background_asking = False
            crons = data.get("session_crons")
            self._cron_prompts = _cron_prompts(crons)
            detail = _own_work(data.get("background_tasks"), crons, self._self_woken)
            if detail and not self._question:
                # It will be woken: not finished, not the user's turn. A
                # question still goes to the user, whatever else runs.
                self._background_since = self._clock()
                self._background_detail = detail
                return self._background_event()
            # In interactive PTY sessions, Stop means Claude finished responding
            # and is waiting for the next user prompt — WAITING is correct here,
            # not COMPLETED (which is reserved for TaskCompleted/SessionEnd).
            return AgentLifecycleEvent(
                status=AgentStatus.WAITING,
                confidence=LifecycleConfidence.HIGH,
                detail="waiting for input",
                question=self._question,
            )
        if event_name == "TaskCompleted":
            if self.waiting_on_background:
                return None  # one task of its list: the agent still waits
            return AgentLifecycleEvent(
                status=AgentStatus.COMPLETED,
                confidence=LifecycleConfidence.HIGH,
                detail="task completed",
            )
        if event_name == "SessionEnd":
            self._background_since = None
            return AgentLifecycleEvent(
                status=AgentStatus.COMPLETED,
                confidence=LifecycleConfidence.HIGH,
                detail="session ended",
            )
        if event_name == "PreToolUse":
            if not data.get("agent_id"):
                self._question = ""
            tool_name = data.get("tool_name", "unknown")
            return AgentLifecycleEvent(
                status=AgentStatus.RUNNING,
                confidence=LifecycleConfidence.HIGH,
                detail=f"using {tool_name}",
            )
        if event_name == "PostToolUse":
            tool_name = data.get("tool_name", "unknown")
            return AgentLifecycleEvent(
                status=AgentStatus.RUNNING,
                confidence=LifecycleConfidence.HIGH,
                detail=f"{tool_name} done",
            )
        return None


def _self_woken(prompt: object, cron_prompts: tuple[str, ...]) -> bool:
    """Whether a submitted prompt is the session waking itself: a background
    task's notification, or the text of one of its crons."""
    if not isinstance(prompt, str):
        return False
    return prompt.lstrip().startswith("<task-notification>") or any(
        prompt.startswith(cron) for cron in cron_prompts
    )


def _cron_prompts(crons: object) -> tuple[str, ...]:
    """The prompts a Stop's ``session_crons`` will submit, without the mark
    of a clipped one."""
    if not isinstance(crons, list):
        return ()
    prompts = (c.get("prompt") for c in crons if isinstance(c, dict))
    return tuple(p.split("\u2026 [+")[0] for p in prompts if isinstance(p, str) and p)


def _own_work(tasks: object, crons: object, self_woken: bool) -> str:
    """What a Stop's turn still waits on of its own (the row's detail), or
    "": it is done.

    Claude's Stop hook lists ``background_tasks`` and ``session_crons`` for
    this. Not counted: type "monitor", the session-long watchers (an
    artifact's live updates) that never end and wake nobody about the work;
    a Monitor on a command is listed as a "shell". A recurring cron counts
    only after a turn it (or another wake-up) started: a turn someone asked
    for can end with the user's turn while a loop ticks on.
    """
    running = [
        t for t in (tasks if isinstance(tasks, list) else ())
        if isinstance(t, dict)
        and t.get("status") in ("running", "pending")
        and t.get("type") != "monitor"
    ]
    if len(running) == 1:
        what = running[0].get("description")
        if isinstance(what, str) and what.strip():
            return f"background work: {' '.join(what.split())[:80]}"
        return "background work"
    if running:
        return f"background work: {len(running)} tasks"
    for cron in crons if isinstance(crons, list) else ():
        if isinstance(cron, dict) and (self_woken or not cron.get("recurring", True)):
            return "waiting for its next wake-up"
    return ""


def _stop_question(data: dict) -> str:
    """The question a Stop hook's turn ends with: from the hook's last
    assistant message, else from the transcript's end."""
    text = data.get("last_assistant_message")
    if not isinstance(text, str):
        transcript = data.get("transcript_path")
        text = last_assistant_text(transcript) if isinstance(transcript, str) and transcript else None
    return turn_question(text) or ""


class CodexAppServerObserver(JsonlTailObserver):
    """Observer that consumes Codex App Server events from a JSONL file."""

    def _map_event(self, data: dict) -> AgentLifecycleEvent | None:
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


class GeminiTelemetryObserver(JsonlTailObserver):
    """Observer that interprets Gemini telemetry streams from a JSONL file."""

    def _map_event(self, data: dict) -> AgentLifecycleEvent | None:
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

    needs_screen = True

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
        elif (
            current_status == AgentStatus.WAITING
            and current_detail == "gemini prompt detected on screen"
        ):
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
