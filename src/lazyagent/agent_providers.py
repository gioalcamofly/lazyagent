from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

SENTINEL_TEXT = "your turn"
SENTINEL_SYSTEM_PROMPT = (
    f"Always output exactly '{SENTINEL_TEXT}' on its own line "
    "when you need user input or have completed your task."
)
DEFAULT_AGENT_PROVIDER = "claude"

# Vars that textual-terminal already sets or that may cause issues if overridden.
ENV_SKIP = frozenset({"TERM", "LC_ALL", "HOME", "_"})


@dataclass(frozen=True)
class AgentProvider:
    """Description of a supported agent CLI."""

    name: str
    executable: str
    dangerous_flag: str
    supports_append_system_prompt: bool = False

    def build_command(self, worktree_path: str, skip_permissions: bool = False) -> str:
        """Build the full shell command used to launch this provider."""
        parts = [self.executable]
        if skip_permissions:
            parts.append(self.dangerous_flag)
        if self.supports_append_system_prompt:
            parts.extend(["--append-system-prompt", SENTINEL_SYSTEM_PROMPT])

        inner_cmd = " ".join(shlex.quote(p) for p in parts)
        script = (
            f"{env_exports()}"
            f" && cd {shlex.quote(worktree_path)}"
            f" && exec {inner_cmd}"
        )
        return f"bash -c {shlex.quote(script)}"


PROVIDERS = {
    "claude": AgentProvider(
        name="claude",
        executable="claude",
        dangerous_flag="--dangerously-skip-permissions",
        supports_append_system_prompt=True,
    ),
    "codex": AgentProvider(
        name="codex",
        executable="codex",
        dangerous_flag="--dangerously-bypass-approvals-and-sandbox",
    ),
    "gemini": AgentProvider(
        name="gemini",
        executable="gemini",
        dangerous_flag="--approval-mode=yolo",
    ),
}


def env_exports() -> str:
    """Build a shell snippet that restores the parent process environment."""
    parts = []
    for key, val in os.environ.items():
        if key in ENV_SKIP:
            continue
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
