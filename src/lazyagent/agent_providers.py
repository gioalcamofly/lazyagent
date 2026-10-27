from __future__ import annotations

import json
import logging
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


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

# Name of the MCP server lazyagent wires into the agents it spawns. Not plain
# "lazyagent": a user-scope server registered under that name (the README's
# setup for standalone sessions) takes precedence over the per-agent one and
# may run another lazyagent install entirely. That server still gets the
# agent's token, from the agent's environment (IDENTITY_ENV).
MCP_SERVER_NAME = "lazyagent_app"
# The per-spawn token the engine maps to the agent (who calls it over MCP).
AGENT_TOKEN_ENV = "LAZYAGENT_AGENT_TOKEN"
# The agent's notes file, for Claude's notes hook (notes_hook.py reads it).
NOTES_PATH_ENV = "LAZYAGENT_NOTES_PATH"
# The engine's MCP socket (mcp_server.py reads it).
SOCKET_ENV = "LAZYAGENT_SOCKET"
# Who an agent is, to its engine. Set in the agent's own environment, not
# only in the MCP config's: Claude starts every stdio MCP server with the
# agent's environment, so a lazyagent server registered by the user (user or
# project scope, tokenless otherwise) knows the caller too. Never inherited:
# an agent spawned from another agent's shell (or a client started there)
# gets its own, never its parent's.
IDENTITY_ENV = frozenset({AGENT_TOKEN_ENV, NOTES_PATH_ENV, SOCKET_ENV})

# Values Claude Code accepts for its crossSessionInbound setting.
CROSS_SESSION_INBOUND_VALUES = ("accept", "hold", "refuse")

# The permission modes an agent can be spawned in ([agents] permission_mode):
# "cli" passes nothing (the CLI's own default, e.g. Claude's
# permissions.defaultMode), "bypass" the provider's dangerous flag, and the
# rest are Claude's --permission-mode values. An unknown value is refused,
# never passed through: a typo would otherwise stop Claude from starting.
PERMISSION_CLI = "cli"
PERMISSION_BYPASS = "bypass"
CLAUDE_PERMISSION_MODES = ("acceptEdits", "plan", "dontAsk", "auto", "default")
PERMISSION_MODES = (PERMISSION_CLI, PERMISSION_BYPASS, *CLAUDE_PERMISSION_MODES)


def check_permission_mode(mode: object) -> str:
    """``mode`` if it is a known permission mode, else ValueError."""
    if mode not in PERMISSION_MODES:
        raise ValueError(
            f"Invalid permission_mode: {mode!r}. "
            f"Must be one of: {', '.join(PERMISSION_MODES)}"
        )
    return mode  # type: ignore[return-value]


def resolve_permission_mode(
    permission_mode: str | None,
    skip_permissions: bool | None,
    configured: str | None,
    default: str = PERMISSION_CLI,
) -> str:
    """The mode an agent is spawned in: an explicit ``permission_mode``,
    else an explicit ``skip_permissions`` (true: bypass; false: the
    configured mode unless that is bypass, else cli), else the configured
    mode, else the caller's ``default``."""
    if permission_mode is not None:
        return check_permission_mode(permission_mode)
    if skip_permissions is True:
        return PERMISSION_BYPASS
    if skip_permissions is False:
        if configured is None or configured == PERMISSION_BYPASS:
            return PERMISSION_CLI
        return configured
    if configured is not None:
        return configured
    return check_permission_mode(default)


def agent_session_name(worktree_path: str, agent_id: str = "") -> str:
    """Name a spawned agent's session, for providers that take one.

    Other Claude sessions address it by this name with SendMessage, so it is
    derived from the worktree folder and the agent id rather than left to the
    CLI's random suffix.
    """
    name = Path(worktree_path).name
    if agent_id:
        name = f"{name}-{agent_id}"
    return re.sub(r"[^A-Za-z0-9._]+", "-", name).strip("-") or "agent"

# Vars that the PTY emulator already sets or that may cause issues if overridden.
# LAZYAGENT_HOST tells a lazyagent TUI it runs inside a host: the agents
# and shells it (or its engine) starts are not hosted, and with a detached
# engine they outlive the host.
ENV_SKIP = frozenset({"TERM", "LC_ALL", "HOME", "_", "LAZYAGENT_HOST"}) | IDENTITY_ENV


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
    instruction_flag: str | None = None
    system_prompt_flag: str | None = None
    session_name_flag: str | None = None
    # Takes one of CLAUDE_PERMISSION_MODES; None: only bypass or not.
    permission_mode_flag: str | None = None

    def permission_args(self, mode: str) -> list[str]:
        """The CLI args for a permission mode (see PERMISSION_MODES)."""
        if mode == PERMISSION_BYPASS:
            return [self.dangerous_flag]
        if mode == PERMISSION_CLI:
            return []
        if self.permission_mode_flag:
            return [self.permission_mode_flag, mode]
        if (self.name, mode) not in _WARNED_MODES:
            _WARNED_MODES.add((self.name, mode))
            log.warning(
                "%s has no permission mode %r; it starts in its own default",
                self.name,
                mode,
            )
        return []

    def build_command(
        self,
        worktree_path: str,
        skip_permissions: bool = False,
        runtime_context: ProviderRuntimeContext | None = None,
        resume_mode: ResumeMode = ResumeMode.NEW,
        instruction: str | None = None,
        system_prompt: str | None = None,
        session_name: str | None = None,
        env: Mapping[str, str] | None = None,
        resume_conversation: str | None = None,
        permission_mode: str | None = None,
    ) -> str:
        """Build the full shell command used to launch this provider.

        ``env`` is the environment the agent starts from (the spawning
        client's); ``None`` means this process's own. ``resume_conversation``
        resumes that conversation id (the pick flag with the id), over
        ``resume_mode``. ``permission_mode`` (PERMISSION_MODES) wins over
        ``skip_permissions``.
        """
        context = runtime_context or self.build_runtime_context(worktree_path)

        resume_args = self._resume_args(resume_mode)
        if resume_conversation and self.resume_pick_args:
            resume_args = (*self.resume_pick_args, resume_conversation)

        if self.resume_is_subcommand and resume_args:
            # Subcommand providers (e.g. codex): executable + resume_args + flags
            parts = [self.executable, *resume_args]
        else:
            parts = [self.executable]
            parts.extend(resume_args)

        if permission_mode is None:
            permission_mode = PERMISSION_BYPASS if skip_permissions else PERMISSION_CLI
        parts.extend(self.permission_args(permission_mode))

        if session_name and self.session_name_flag:
            parts.extend([self.session_name_flag, session_name])

        if system_prompt:
            if self.system_prompt_flag:
                parts.extend([self.system_prompt_flag, system_prompt])
            elif instruction:
                # Providers without a dedicated flag: prepend to instruction
                instruction = system_prompt + "\n\n" + instruction
            else:
                instruction = system_prompt

        if instruction:
            if self.instruction_flag:
                parts.extend([self.instruction_flag, instruction])
            else:
                parts.append(instruction)

        if "settings_path" in context.metadata:
            parts.extend(["--settings", context.metadata["settings_path"]])
        if "mcp_config_path" in context.metadata:
            parts.extend(["--mcp-config", context.metadata["mcp_config_path"]])
        inner_cmd = " ".join(shlex.quote(p) for p in parts)
        script = (
            f"{env_exports(context.env_overrides, base=env)}"
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

    def build_runtime_context(
        self,
        worktree_path: str,
        socket_path: str | None = None,
        cross_session_inbound: str | None = None,
        agent_token: str | None = None,
        notes_path: str | None = None,
    ) -> ProviderRuntimeContext:
        """Build provider-specific runtime metadata for the spawned session.

        ``cross_session_inbound`` only applies to Claude, the one provider
        with cross-session messaging. ``agent_token`` goes into the MCP
        server's env and the agent's own (``LAZYAGENT_AGENT_TOKEN``, see
        ``IDENTITY_ENV``): how the engine tells which agent calls it (the
        notes tools' "my notes"). ``notes_path`` (the
        agent's notes file) is for Claude's notes hook, the one provider
        whose hooks can re-inject them.
        """
        if self.name == "claude":
            return _build_claude_runtime_context(
                self,
                worktree_path,
                socket_path,
                cross_session_inbound,
                agent_token,
                notes_path,
            )
        if self.name == "codex":
            return _build_codex_runtime_context(
                self, worktree_path, socket_path, agent_token
            )
        if self.name == "gemini":
            return _build_gemini_runtime_context(
                self, worktree_path, socket_path, agent_token
            )
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


# (provider, mode) pairs already warned about: one log line each.
_WARNED_MODES: set[tuple[str, str]] = set()

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
        system_prompt_flag="--append-system-prompt",
        session_name_flag="--name",
        permission_mode_flag="--permission-mode",
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
        instruction_flag="-i",
    ),
}


def env_exports(
    extra_env: dict[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> str:
    """Build a shell snippet that restores an environment in the child.

    The PTY child starts with almost nothing (see ``PtyEmulator``), so this
    is what it really runs with: ``base`` (this process's environment when
    ``None``) plus ``extra_env``.
    """
    source = os.environ if base is None else base
    merged = {
        key: val
        for key, val in source.items()
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


def _agent_env(socket_path: str | None, agent_token: str | None) -> dict[str, str]:
    """The engine's socket and the agent's token (see ``IDENTITY_ENV``)."""
    if not socket_path:
        return {}
    env = {SOCKET_ENV: socket_path}
    if agent_token:
        env[AGENT_TOKEN_ENV] = agent_token
    return env


def _mcp_env(socket_path: str, agent_token: str | None) -> dict[str, str]:
    """The lazyagent MCP server's environment."""
    return {"PYTHONUNBUFFERED": "1", **_agent_env(socket_path, agent_token)}


def _build_gemini_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
    socket_path: str | None = None,
    agent_token: str | None = None,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-gemini-telemetry-"))
    telemetry_log_path = temp_dir / "telemetry.jsonl"

    env_overrides = {
        "GEMINI_TELEMETRY_LOG": str(telemetry_log_path),
        **_agent_env(socket_path, agent_token),
    }

    if socket_path:
        # Gemini CLI reads settings.json from $GEMINI_CLI_HOME/.gemini/.
        # Point it at a temp directory containing only our MCP entry.
        gemini_home = temp_dir / "gemini_home"
        (gemini_home / ".gemini").mkdir(parents=True, exist_ok=True)
        # Use sys.executable (the interpreter running lazyagent) rather
        # than "python3" — for pipx/isolated-venv installs, PATH's
        # `python3` is the system one and doesn't have lazyagent
        # installed, so the MCP server would fail to start.
        settings = {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": sys.executable,
                    "args": ["-m", "lazyagent.mcp_server"],
                    "env": _mcp_env(socket_path, agent_token),
                }
            }
        }
        (gemini_home / ".gemini" / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )
        env_overrides["GEMINI_CLI_HOME"] = str(gemini_home)

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,

        env_overrides=env_overrides,
        metadata={
            "temp_dir": str(temp_dir),
            "telemetry_log_path": str(telemetry_log_path),
        },
    )


def _build_codex_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
    socket_path: str | None = None,
    agent_token: str | None = None,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-codex-events-"))
    app_server_log_path = temp_dir / "app-server-events.jsonl"

    # NOTE: In a real implementation we might start a sidecar process here
    # or use a Codex-specific flag to pipe events to this file.
    # For now, we prepare the environment so Codex knows where to log.

    env_overrides = {
        "CODEX_APP_SERVER_LOG": str(app_server_log_path),
        **_agent_env(socket_path, agent_token),
    }

    if socket_path:
        # Codex reads MCP server config from $CODEX_HOME/config.toml.
        # Point it at a temp directory containing only our MCP entry.
        codex_home = temp_dir / "codex_home"
        codex_home.mkdir(exist_ok=True)
        # See note in the gemini context builder: sys.executable handles
        # pipx/isolated-venv installs that "python3" misses.
        config_toml = (
            f'[mcp_servers.{MCP_SERVER_NAME}]\n'
            f'command = {json.dumps(sys.executable)}\n'
            f'args = ["-m", "lazyagent.mcp_server"]\n'
            f'\n[mcp_servers.{MCP_SERVER_NAME}.env]\n'
            + "".join(
                f"{key} = {json.dumps(value)}\n"
                for key, value in _mcp_env(socket_path, agent_token).items()
            )
        )
        (codex_home / "config.toml").write_text(config_toml, encoding="utf-8")
        env_overrides["CODEX_HOME"] = str(codex_home)

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,

        env_overrides=env_overrides,
        metadata={
            "temp_dir": str(temp_dir),
            "app_server_log_path": str(app_server_log_path),
        },
    )


def _build_claude_runtime_context(
    provider: AgentProvider,
    worktree_path: str,
    socket_path: str | None = None,
    cross_session_inbound: str | None = None,
    agent_token: str | None = None,
    notes_path: str | None = None,
) -> ProviderRuntimeContext:
    temp_dir = Path(tempfile.mkdtemp(prefix="lazyagent-claude-hooks-"))
    hook_log_path = temp_dir / "hook-events.jsonl"
    hook_script_path = temp_dir / "log_hook.py"
    settings_path = temp_dir / "hooks-settings.json"

    hook_script_path.write_text(_claude_hook_script(), encoding="utf-8")
    hook_script_path.chmod(hook_script_path.stat().st_mode | stat.S_IXUSR)

    hook_command = {"type": "command", "command": str(hook_script_path)}
    # On SessionStart and UserPromptSubmit; SessionStart is logged too: it
    # brings the conversation id before the first prompt (and the new one
    # right after a /clear).
    notes_hooks = [hook_command]
    env_overrides = {
        "LAZYAGENT_CLAUDE_HOOK_LOG": str(hook_log_path),
        **_agent_env(socket_path, agent_token),
    }
    metadata = {
        "temp_dir": str(temp_dir),
        "hook_log_path": str(hook_log_path),
        "hook_script_path": str(hook_script_path),
        "settings_path": str(settings_path),
    }
    if notes_path:
        # Copied, not run from the package: like log_hook.py it must work
        # with whatever python3 the agent finds (pipx installs included).
        notes_hook_path = temp_dir / "notes_hook.py"
        notes_hook_path.write_text(_notes_hook_script(), encoding="utf-8")
        notes_hook_path.chmod(notes_hook_path.stat().st_mode | stat.S_IXUSR)
        notes_hooks.append({"type": "command", "command": str(notes_hook_path)})
        env_overrides[NOTES_PATH_ENV] = notes_path
        metadata["notes_hook_path"] = str(notes_hook_path)

    hooks_settings: dict = {
        "hooks": {
            "Notification": [
                {
                    "matcher": "permission_prompt|idle_prompt|elicitation_dialog",
                    "hooks": [hook_command],
                }
            ],
            "SessionStart": [{"hooks": notes_hooks}],
            "UserPromptSubmit": [{"hooks": notes_hooks}],
            "Stop": [{"hooks": [hook_command]}],
            "TaskCompleted": [{"hooks": [hook_command]}],
            "SessionEnd": [{"hooks": [hook_command]}],
            "PreToolUse": [{"hooks": [hook_command]}],
            "PostToolUse": [{"hooks": [hook_command]}],
        }
    }

    if cross_session_inbound:
        # Claude Code holds a SendMessage for approval when the sender runs in
        # another permission mode; "accept" delivers it anyway.
        hooks_settings["crossSessionInbound"] = cross_session_inbound

    if socket_path:
        # Claude Code reads MCP servers from --mcp-config files, not from a
        # --settings file (an mcpServers key there is silently ignored).
        # sys.executable, not "python3": pipx/isolated-venv installs
        # don't expose lazyagent on the system python, so a bare "python3"
        # invocation in the agent's environment would ImportError.
        mcp_config_path = temp_dir / "mcp-config.json"
        mcp_config = {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": sys.executable,
                    "args": ["-m", "lazyagent.mcp_server"],
                    "env": _mcp_env(socket_path, agent_token),
                }
            }
        }
        mcp_config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
        metadata["mcp_config_path"] = str(mcp_config_path)
        duplicates = lazyagent_mcpjson_servers(worktree_path)
        if duplicates:
            # The repo's own lazyagent server (.mcp.json) would be a second
            # copy of the tools above, tokenless, one more process tree per
            # agent. Rejected by name: the repo's other servers still run
            # (--strict-mcp-config would drop them and the user's too).
            hooks_settings["disabledMcpjsonServers"] = duplicates

    settings_path.write_text(json.dumps(hooks_settings), encoding="utf-8")

    return ProviderRuntimeContext(
        provider_name=provider.name,
        worktree_path=worktree_path,
        observation_mode=provider.observation_mode,
        env_overrides=env_overrides,
        metadata=metadata,
    )


def _runs_lazyagent_mcp(server: object) -> bool:
    """Whether an MCP server entry starts lazyagent's MCP server:
    ``python -m lazyagent.mcp_server`` or ``lazyagent mcp``, through any
    wrapper (``uv run``, ``uvx``)."""
    if not isinstance(server, dict):
        return False
    command = server.get("command")
    args = server.get("args")
    words = [command] if isinstance(command, str) else []
    if isinstance(args, list):
        words += [a for a in args if isinstance(a, str)]
    if any("lazyagent.mcp_server" in w for w in words):
        return True
    return any(
        Path(w).name == "lazyagent" and nxt == "mcp" for w, nxt in zip(words, words[1:])
    )


def lazyagent_mcpjson_servers(worktree_path: str) -> list[str]:
    """The servers of the worktree's ``.mcp.json`` that run lazyagent's MCP
    server, by name; [] when there is no such file or it does not parse."""
    try:
        data = json.loads((Path(worktree_path) / ".mcp.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(name for name, server in servers.items() if _runs_lazyagent_mcp(server))


def _notes_hook_script() -> str:
    return Path(__file__).with_name("notes_hook.py").read_text(encoding="utf-8")


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
