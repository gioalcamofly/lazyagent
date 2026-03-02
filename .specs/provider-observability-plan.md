# Lazyagent: Provider Observability Plan

## Current Status

The first implementation slice for this plan is already in progress on branch `feat/provider-observability`.

Implemented so far:
- `src/lazyagent/agent_providers.py` now carries provider observability metadata in addition to command-building logic
- `src/lazyagent/agent_observers.py` exists and defines a normalized observer layer
- `src/lazyagent/widgets/monitored_terminal.py` now delegates lifecycle detection to observers instead of hardcoding all logic internally
- the terminal sentinel behavior has been preserved as a fallback observer
- Claude now has an initial hooks-based runtime context and `ClaudeHooksObserver`, layered together with the sentinel fallback through a composite observer

Current limitations:
- the app still uses the existing coarse `AgentStatus` model (`RUNNING`, `WAITING`, `POSSIBLY_HANGED`, `NO_AGENT`)
- Claude hook events are currently mapped into the existing status model rather than richer normalized statuses
- Codex and Gemini still use the terminal fallback observer only
- full `pytest` verification is still blocked in the current sandbox, although the touched modules compile successfully with `python3 -m compileall src/lazyagent tests`

## Immediate Next Steps

1. Commit the current observability scaffold and Claude hooks slice on `feat/provider-observability`.
2. Introduce a richer internal lifecycle/status model that can represent approval waits, user waits, completion, and failure without changing the visible UI all at once.
3. Add a Codex structured observer path, starting with transport and event-shape abstractions for app-server integration while preserving terminal fallback.
4. Add a Gemini telemetry runtime context and observer scaffold, again keeping terminal fallback in place.
5. Once provider-specific observers are stable, update the UI to distinguish richer states such as approval waits versus general waiting.

## Context

`lazyagent` currently treats agent lifecycle detection as a terminal-screen problem: it launches a provider CLI in a PTY, scans the rendered screen for `"your turn"`, and maps that to `WAITING`. That works best for Claude because Claude supports `--append-system-prompt`, which lets the app explicitly ask for the sentinel text.

Now that the app supports `claude`, `codex`, and `gemini`, that shared sentinel approach is no longer a good primary abstraction:
- `claude` exposes structured hooks
- `codex` exposes structured machine-readable events and an app server
- `gemini` exposes machine-readable output and telemetry, but not the same hook/event surface

The existing provider interface in `src/lazyagent/agent_providers.py` is the right seam to evolve. Today it owns:
- provider normalization
- supported provider registry
- command construction

This plan extends it so it also owns provider capabilities and observability strategy.

## Research Summary

### Claude

Official sources show three useful surfaces:
- CLI flags such as `--append-system-prompt` and JSON/streaming output modes
- Hooks, including `Notification`, `Stop`, `TaskCompleted`, and `SessionEnd`
- Hook payloads including `session_id`, `cwd`, `transcript_path`, and event-specific context

Implication:
- the current sentinel prompt can remain as fallback
- hooks should become the primary lifecycle signal for interactive Claude sessions

### Codex

Official sources show two strong structured surfaces:
- `codex exec --json`, which emits JSONL events like `turn.started`, `turn.completed`, `turn.failed`, and `item.*`
- Codex App Server, which exposes JSON-RPC events such as `thread/status/changed`, `turn/started`, `turn/completed`, `item/agentMessage/delta`, and approval-related state like `waitingOnApproval`

Implication:
- Codex should not rely on screen sentinel text as the primary lifecycle mechanism
- app-server integration should be the preferred path when available

### Gemini

Official sources show:
- headless prompt mode with `--output-format json`
- `GEMINI.md` for persistent context
- checkpoint/session persistence
- telemetry via OpenTelemetry or file export, including prompt, tool-call, file-operation, request, response, and error data

Implication:
- Gemini does not currently offer an equally strong hook/app-server surface in the official docs
- telemetry and JSON output are the best structured signals available today
- terminal/sentinel parsing should remain a fallback for interactive Gemini sessions

## Goal

Move from a single shared sentinel detector to a provider-aware observability model:
- Claude: hooks-first
- Codex: structured events-first
- Gemini: telemetry-first
- Shared fallback: terminal/screen sentinel parsing

The user-facing result should be better agent state reporting with less provider-specific fragility.

## Non-Goals

- Replace the PTY-based interactive UI
- Remove the terminal fallback path entirely
- Fully re-architect the whole app around JSON-only execution
- Solve every provider-specific nuance in one pass

## Design Principles

### Keep The Current Provider Interface

Do not discard `src/lazyagent/agent_providers.py`. Extend it.

Each provider should describe:
- how to launch
- what capabilities it supports
- how it should be observed
- what fallback behavior is acceptable

### Normalize Statuses Across Providers

The app should not reason in provider-specific states. It should consume a normalized status model:
- `running`
- `waiting_for_user`
- `waiting_for_approval`
- `completed`
- `failed`
- `interrupted`

Each event should also carry:
- `confidence`: `high`, `medium`, or `low`
- optional `detail`
- optional provider-native metadata

### Prefer Official Structured Signals Over Prompt Conventions

Order of trust:
1. official hooks / app-server / machine-readable events
2. telemetry / structured logs
3. terminal screen inference
4. prompt-based sentinel text

## Proposed Architecture

### 1. Extend `agent_providers.py`

Add provider capabilities and observer creation to the existing interface.

Suggested shape:

```python
@dataclass(frozen=True)
class AgentProvider:
    name: str
    executable: str
    dangerous_flag: str
    supports_append_system_prompt: bool = False
    observation_mode: str = "terminal"
    supports_structured_turn_events: bool = False
    supports_approval_events: bool = False
    supports_completion_events: bool = False

    def build_command(...) -> str: ...
    def build_runtime_context(...) -> ProviderRuntimeContext: ...
    def create_observer(...) -> AgentObserver: ...
```

Possible observation modes:
- `terminal`
- `hooks`
- `app_server`
- `telemetry`

Add `ProviderRuntimeContext` to hold paths, temp files, environment overrides, and provider-local config needed at spawn time.

### 2. Add `agent_observers.py`

Introduce a provider observer layer that converts provider-native signals into normalized app events.

Suggested types:

```python
@dataclass
class AgentLifecycleEvent:
    status: str
    confidence: str
    detail: str = ""
    metadata: dict[str, str] | None = None

class AgentObserver:
    def on_process_started(self) -> list[AgentLifecycleEvent]: ...
    def on_terminal_output(self, chars: str) -> list[AgentLifecycleEvent]: ...
    def poll(self) -> list[AgentLifecycleEvent]: ...
    def on_disconnect(self) -> list[AgentLifecycleEvent]: ...
    def cleanup(self) -> None: ...
```

Implementations:
- `ClaudeHooksObserver`
- `CodexAppServerObserver`
- `GeminiTelemetryObserver`
- `TerminalSentinelObserver`
- optionally `CompositeObserver`

### 3. Refactor `MonitoredTerminal`

`src/lazyagent/widgets/monitored_terminal.py` should stop owning provider-specific detection logic.

Instead it should:
- accept an `AgentObserver`
- forward stdout chunks to the observer
- apply returned lifecycle events to app state
- keep the hang timer only as a fallback safety net

The current screen-based `"your turn"` logic should move into `TerminalSentinelObserver`.

### 4. Keep The App Provider-Agnostic

`src/lazyagent/app.py` and `src/lazyagent/widgets/center_panel.py` should only deal with:
- provider selection
- spawning a terminal with provider runtime config
- consuming normalized lifecycle events

The UI should not know whether a state came from a Claude hook, a Codex app-server event, or a Gemini telemetry file.

## Provider Strategies

## Phase A: Claude Hooks Adapter

Primary path:
- install a temporary or repo-local hook configuration for the spawned Claude session
- write hook events to a JSONL file in the worktree or temp directory
- have `ClaudeHooksObserver` tail that file and map events into normalized statuses

Event mapping:
- `permission_prompt` -> `waiting_for_approval`
- `idle_prompt` -> `waiting_for_user`
- `elicitation_dialog` -> `waiting_for_user`
- `TaskCompleted` -> `completed`
- `Stop` -> `completed`
- `SessionEnd` with non-success context -> `failed` or `interrupted`

Fallback:
- keep `--append-system-prompt` and terminal sentinel parsing if hooks are unavailable

Why first:
- Claude has the cleanest official migration path away from the sentinel trick while preserving the current interactive workflow

## Phase B: Codex Structured Adapter

Primary path:
- use Codex App Server when available
- connect to its structured event stream
- map turn and approval events directly into app state

Fallback path:
- if app-server integration is not available yet, keep interactive PTY mode and terminal fallback
- optionally add `codex exec --json` support for non-interactive or batch flows later

Event mapping:
- `turn/started` -> `running`
- `thread/status/changed` with `waitingOnApproval` -> `waiting_for_approval`
- `turn/completed` -> `completed`
- failure events -> `failed`

Why second:
- Codex exposes the richest official lifecycle surface, but integrating app-server transport is a larger change than Claude hooks

## Phase C: Gemini Telemetry Adapter

Primary path:
- launch Gemini with telemetry or file-export configuration enabled
- emit telemetry into a local file owned by the session
- have `GeminiTelemetryObserver` watch and interpret the stream

Event mapping:
- tool activity / request / response -> `running`
- explicit error telemetry -> `failed`
- session-end or final-response heuristics -> `completed`
- approval/waiting state remains best-effort unless Gemini exposes stronger official signals later

Fallback:
- terminal sentinel parsing
- optional `GEMINI.md` convention if experimentation proves it useful, but do not treat it as a strong contract

Why third:
- Gemini has useful structured data, but not a hook/app-server surface on par with Claude or Codex

## Shared Fallback Strategy

Keep `TerminalSentinelObserver` as the compatibility layer for all providers.

Responsibilities:
- scan rendered screen contents, not raw stdout
- detect configured sentinel phrases
- downgrade confidence to `low`
- cooperate with hang detection

This is still valuable because:
- it provides a universal fallback
- it preserves current behavior during migration
- it covers degraded environments where structured integration fails

## File-By-File Roadmap

### New Files

```text
src/lazyagent/agent_observers.py
src/lazyagent/provider_runtime.py
tests/test_agent_observers.py
```

### Files To Refactor

```text
src/lazyagent/agent_providers.py
src/lazyagent/widgets/monitored_terminal.py
src/lazyagent/widgets/center_panel.py
src/lazyagent/app.py
tests/test_center_panel.py
tests/test_monitored_terminal.py
tests/test_app_integration.py
README.md
```

## Incremental Delivery Plan

### Step 1: Capability Model

Extend `AgentProvider` with:
- observation mode
- structured-event capabilities
- runtime-context builder
- observer factory

This should land without changing visible behavior.

### Step 2: Observer Abstraction

Add the normalized event model and the observer interface.

Initially wire all providers to `TerminalSentinelObserver` so the refactor is behavior-preserving.

### Step 3: Delegate `MonitoredTerminal`

Refactor `MonitoredTerminal` to delegate lifecycle detection to the active observer.

The terminal widget should remain the PTY owner, but not the provider-specific decision maker.

### Step 4: Claude Hooks

Implement and ship `ClaudeHooksObserver`.

This should be the first provider-specific upgrade because it can immediately reduce sentinel dependence for the default provider.

### Step 5: Codex Structured Events

Implement and ship `CodexAppServerObserver`.

If app-server integration proves too large for one PR, split it:
- part 1: capability scaffolding and transport abstraction
- part 2: event mapping and UI wiring

### Step 6: Gemini Telemetry

Implement and ship `GeminiTelemetryObserver`.

This phase should be explicit about confidence levels and likely remain partially heuristic.

### Step 7: UI Improvements

Use the richer normalized statuses in the sidebar and notifications:
- distinguish `waiting_for_approval` from `waiting_for_user`
- show confidence-sensitive wording if useful
- avoid marking providers as "hung" when a higher-confidence observer says they are intentionally waiting

## Testing Plan

### Unit Tests

Add direct tests for:
- provider capability selection
- runtime-context generation
- observer creation
- event normalization
- fallback selection when structured mode is unavailable

### Observer Tests

Claude:
- hook file event parsing
- permission/user/completion mapping

Codex:
- app-server event mapping
- approval/completion/failure mapping

Gemini:
- telemetry event parsing
- activity/error/completion heuristics

Fallback:
- rendered-screen sentinel detection remains intact

### Integration Tests

Validate:
- `WorktreePanel.spawn_agent()` selects provider observer correctly
- `MonitoredTerminal` delegates detection rather than hardcoding provider assumptions
- app state transitions use normalized lifecycle events

## Risks

### Provider Drift

Provider CLIs and event surfaces may change over time.

Mitigation:
- keep capabilities explicit in `AgentProvider`
- isolate provider-native parsing in observer classes
- prefer official documented surfaces over terminal conventions

### Too Much Complexity At Once

Trying to ship hooks, app-server, telemetry, and UI changes together would be risky.

Mitigation:
- land the observer abstraction first with zero behavior change
- upgrade one provider at a time

### Gemini Ambiguity

Gemini currently appears to have weaker interactive lifecycle signaling than Claude/Codex.

Mitigation:
- keep confidence levels in normalized events
- preserve fallback terminal detection
- avoid over-promising on exact Gemini waiting semantics

## Recommended Execution Order

1. Extend `AgentProvider` with observability capabilities
2. Introduce `AgentObserver` and normalized lifecycle events
3. Refactor `MonitoredTerminal` to delegate to observers
4. Ship `ClaudeHooksObserver`
5. Ship `CodexAppServerObserver`
6. Ship `GeminiTelemetryObserver`
7. Improve UI status wording and confidence handling

## Acceptance Criteria

This plan is complete when:
- provider lifecycle detection is no longer hardcoded in `MonitoredTerminal`
- `agent_providers.py` owns both launch and observability strategy
- Claude no longer relies primarily on the `"your turn"` sentinel
- Codex can use structured turn/approval/completion events
- Gemini can use telemetry-based observation with explicit fallback behavior
- terminal/screen sentinel detection remains available as a universal compatibility path

## Sources

- Claude CLI usage: https://docs.anthropic.com/en/docs/claude-code/cli-usage
- Claude hooks: https://docs.anthropic.com/en/docs/claude-code/hooks
- Codex non-interactive mode: https://developers.openai.com/codex/noninteractive
- Codex App Server: https://developers.openai.com/codex/app-server
- Gemini headless mode: https://google-gemini.github.io/gemini-cli/docs/cli/headless.html
- Gemini configuration: https://google-gemini.github.io/gemini-cli/docs/get-started/configuration.html
- Gemini `GEMINI.md`: https://google-gemini.github.io/gemini-cli/docs/cli/gemini-md.html
- Gemini checkpointing: https://google-gemini.github.io/gemini-cli/docs/checkpointing.html
- Gemini telemetry: https://google-gemini.github.io/gemini-cli/docs/cli/telemetry.html
- AgentAPI terminal observation reference: https://github.com/coder/agentapi
- Agent Sessions reference: https://github.com/jazzyalex/agent-sessions
