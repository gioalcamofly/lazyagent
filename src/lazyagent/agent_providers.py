from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ObservationMode(Enum):
    TERMINAL = "terminal"
    HOOKS = "hooks"
    APP_SERVER = "app_server"
    TELEMETRY = "telemetry"


class ResumeMode(Enum):
    NEW = "new"
    RESUME_PICK = "pick"
    RESUME_LAST = "last"

DEFAULT_AGENT_PROVIDER = "claude"

# Vars that the PTY emulator already sets or that may cause issues if overridden.
ENV_SKIP = frozenset({"TERM", "LC_ALL", "HOME", "_"})


@dataclass(frozen=True)
class ProviderRuntimeContext:
    """Spawn-time runtime context for provider command + observability."""

    provider_name: str
    worktree_path: str
    observation_mode: ObservationMode
    env_overrides: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentProvider:
    """Description of a supported agent CLI."""

    name: str
    executable: str
    dangerous_flag: str
    observation_mode: ObservationMode = ObservationMode.TERMINAL
    supports_structured_turn_events: bool = False
    supports_approval_events: bool = False
    supports_completion_events: bool = False
    resume_pick_args: tuple[str, ...] = ()
    resume_last_args: tuple[str, ...] = ()
    resume_is_subcommand: bool = False

    def build_command(
        self,
        worktree_path: str,
        skip_permissions: bool = False,
        runtime_context: ProviderRuntimeContext | None = None,
        resume_mode: ResumeMode = ResumeMode.NEW,
    ) -> str:
        """Build the full shell command used to launch this provider."""
        context = runtime_context or self.build_runtime_context(worktree_path)

        resume_args = self._resume_args(resume_mode)

        if self.resume_is_subcommand and resume_args:
            # Subcommand providers (e.g. codex): executable + resume_args + flags
            parts = [self.executable, *resume_args]
        else:
            parts = [self.executable]
            parts.extend(resume_args)

        if skip_permissions:
            parts.append(self.dangerous_flag)

        if "settings_path" in context.metadata:
            parts.extend(["--settings", context.metadata["settings_path"]])
        inner_cmd = " ".join(shlex.quote(p) for p in parts)
        script = (
            f"{env_exports(context.env_overrides)}"
            f" && cd {shlex.quote(worktree_path)}"
            f" && exec {inner_cmd}"
        )
        return f"bash -c {shlex.quote(script)}"

    def _resume_args(self, resume_mode: ResumeMode) -> tuple[str, ...]:
        """Return CLI args for the given resume mode."""
        if resume_mode == ResumeMode.RESUME_PICK:
            return self.resume_pick_args
        if resume_mode == ResumeMode.RESUME_LAST:
            return self.resume_last_args
        return ()

    def build_runtime_context(self, worktree_path: str) -> ProviderRuntimeContext:
        """Build provider-specific runtime metadata for the spawned session."""
        if self.name == "claude":
            return _build_claude_runtime_context(self, worktree_path)
        if self.name == "codex":
            return _build_codex_runtime_context(self, worktree_path)
        if self.name == "gemini":
            return _build_gemini_runtime_context(self, worktree_path)
        return ProviderRuntimeContext(
            provider_name=self.name,
            worktree_path=worktree_path,
            observation_mode=self.observation_mode,
        )

    def create_observer(self, worktree_path: str):
        """Create the lifecycle observer for this provider."""
        context = self.build_runtime_context(worktree_path)
        return self.create_observer_from_context(context)

    def create_observer_from_context(self, context: ProviderRuntimeContext):
        """Create the lifecycle observer for the prepared runtime context."""
        from lazyagent.agent_observers import (
            ClaudeHooksObserver,
            CodexAppServerObserver,
            CompositeObserver,
            GeminiTelemetryObserver,
            GeminiPromptObserver,
        )

        observers = []
        if self.name == "claude":
            observers.append(
                ClaudeHooksObserver(
                    context.metadata["hook_log_path"],
                    temp_dir=context.metadata["temp_dir"],
                )
            )
        elif self.name == "codex":
            observers.append(
                CodexAppServerObserver(
                    context.metadata["app_server_log_path"],
                    temp_dir=context.metadata["temp_dir"],
                )
            )
        elif self.name == "gemini":
            observers.append(
                GeminiTelemetryObserver(
                    context.metadata["telemetry_log_path"],
                    temp_dir=context.metadata["temp_dir"],
                )
            )
            observers.append(GeminiPromptObserver())

        if len(observers) == 1:
            return observers[0]
        return CompositeObserver(observers)


PROVIDERS = {
    "claude": AgentProvider(
        name="claude",
        executable="claude",
        dangerous_flag="--dangerously-skip-permissions",
        observation_mode=ObservationMode.HOOKS,
        supports_approval_events=True,
        supports_completion_events=True,
        resume_pick_args=("--resume",),
        resume_last_args=("--continue",),
    ),
    "codex": AgentProvider(
        name="codex",
        executable="codex",
        dangerous_flag="--dangerously-bypass-approvals-and-sandbox",
        observation_mode=ObservationMode.APP_SERVER,
        supports_structured_turn_events=True,
        supports_approval_events=True,
        supports_completion_events=True,
        resume_pick_args=("resume",),
        resume_last_args=("resume", "--last"),
        resume_is_subcommand=True,
    ),
    "gemini": AgentProvider(
        name="gemini",
        executable="gemini",
        dangerous_flag="--approval-mode=yolo",
        observation_mode=ObservationMode.TELEMETRY,
        supports_completion_events=True,
        resume_pick_args=("--resume",),
        resume_last_args=("--resume",),
    ),
}


def env_exports(extra_env: dict[str, str] | None = None) -> str:
    """Build a shell snippet that restores the parent process environment."""
    merged = {
        key: val
        for key, val in os.environ.items()
        if key not in ENV_SKIP
    }
    if extra_env:
        merged.update(extra_env)

    parts = []
    for key, val in merged.items():
        parts.append(f"{key}={shlex.quote(val)}")
    return "export " + " ".join(parts) if parts else "true"


def normalize_provider_name(provider: str | None) -> str:
    """Normalize a configured provider name to a supported identifier."""
    candidate = (provider or DEFAULT_AGENT_PROVIDER).strip().lower()
    if candidate not in PROVIDERS:
        return DEFAULT_AGENT_PROVIDER
    return candidate


def get_agent_provider(provider: str | None) -> AgentProvider:
    """Return the supported provider for the given config value."""
    return PROVIDERS[normalize_provider_name(provider)]


def _build_gemini_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-gemini-telemetry-"))
    telemetry_log_path = temp_dir / "telemetry.jsonl"

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,

        env_overrides={
            "GEMINI_TELEMETRY_LOG": str(telemetry_log_path),
        },
        metadata={
            "temp_dir": str(temp_dir),
            "telemetry_log_path": str(telemetry_log_path),
        },
    )


def _build_codex_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-codex-events-"))
    app_server_log_path = temp_dir / "app-server-events.jsonl"

    # NOTE: In a real implementation we might start a sidecar process here
    # or use a Codex-specific flag to pipe events to this file.
    # For now, we prepare the environment so Codex knows where to log.

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,

        env_overrides={
            "CODEX_APP_SERVER_LOG": str(app_server_log_path),
        },
        metadata={
            "temp_dir": str(temp_dir),
            "app_server_log_path": str(app_server_log_path),
        },
    )


def _build_claude_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-claude-hooks-"))
    hook_log_path = temp_dir / "hook-events.jsonl"
    hook_script_path = temp_dir / "log_hook.py"
    settings_path = temp_dir / "hooks-settings.json"

    hook_script_path.write_text(_claude_hook_script(), encoding="utf-8")
    hook_script_path.chmod(hook_script_path.stat().st_mode | stat.S_IXUSR)

    hook_command = {"type": "command", "command": str(hook_script_path)}

    hooks_settings = {
        "hooks": {
            "Notification": [
                {
                    "matcher": "permission_prompt|idle_prompt|elicitation_dialog",
                    "hooks": [hook_command],
                }
            ],
            "Stop": [{"hooks": [hook_command]}],
            "TaskCompleted": [{"hooks": [hook_command]}],
            "SessionEnd": [{"hooks": [hook_command]}],
            "PreToolUse": [{"hooks": [hook_command]}],
            "PostToolUse": [{"hooks": [hook_command]}],
        }
    }
    settings_path.write_text(json.dumps(hooks_settings), encoding="utf-8")

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,
        env_overrides={
            "LAZYAGENT_CLAUDE_HOOK_LOG": str(hook_log_path),
        },
        metadata={
            "temp_dir": str(temp_dir),
            "hook_log_path": str(hook_log_path),
            "hook_script_path": str(hook_script_path),
            "settings_path": str(settings_path),
        },
    )


def _claude_hook_script() -> str:
    return """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

payload = sys.stdin.read()
if not payload.strip():
    raise SystemExit(0)

try:
    data = json.loads(payload)
except json.JSONDecodeError:
    raise SystemExit(0)

log_path = os.environ.get("LAZYAGENT_CLAUDE_HOOK_LOG")
if not log_path:
    raise SystemExit(0)

path = Path(log_path)
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(data) + "\\n")
"""
