from __future__ import annotations

import json
import shlex
from lazyagent.agent_observers import (
    ClaudeHooksObserver,
    CodexAppServerObserver,
)
from lazyagent.agent_providers import (
    DEFAULT_AGENT_PROVIDER,
    ObservationMode,
    get_agent_provider,
    normalize_provider_name,
)


class TestNormalizeProviderName:
    def test_defaults_to_claude_for_none(self):
        assert normalize_provider_name(None) == DEFAULT_AGENT_PROVIDER

    def test_normalizes_case_and_whitespace(self):
        assert normalize_provider_name("  CoDeX  ") == "codex"

    def test_normalizes_gemini(self):
        assert normalize_provider_name(" Gemini ") == "gemini"

    def test_invalid_provider_falls_back_to_default(self):
        assert normalize_provider_name("geminix") == DEFAULT_AGENT_PROVIDER


class TestGetAgentProvider:
    def test_claude_uses_hooks_observation(self):
        provider = get_agent_provider("claude")
        assert provider.executable == "claude"
        assert provider.observation_mode == ObservationMode.HOOKS

    def test_codex_uses_own_dangerous_flag(self):
        provider = get_agent_provider("codex")
        assert provider.executable == "codex"
        assert (
            provider.dangerous_flag
            == "--dangerously-bypass-approvals-and-sandbox"
        )
        assert provider.observation_mode == ObservationMode.APP_SERVER
        assert provider.supports_structured_turn_events is True

    def test_gemini_uses_approval_mode_yolo(self):
        provider = get_agent_provider("gemini")
        assert provider.executable == "gemini"
        assert provider.dangerous_flag == "--approval-mode=yolo"
        assert provider.observation_mode == ObservationMode.TELEMETRY

    def test_invalid_provider_returns_default_provider(self):
        provider = get_agent_provider("other")
        assert provider.name == DEFAULT_AGENT_PROVIDER


class TestBuildCommand:
    def test_claude_command_does_not_append_system_prompt(self):
        command = get_agent_provider("claude").build_command("/tmp/wt")
        script = shlex.split(command)[2]
        assert "--append-system-prompt" not in script

    def test_gemini_command_uses_yolo_flag_when_requested(self):
        command = get_agent_provider("gemini").build_command(
            "/tmp/wt",
            skip_permissions=True,
        )
        script = shlex.split(command)[2]
        assert "exec gemini" in script
        assert "--approval-mode=yolo" in script


class TestRuntimeContext:
    def test_build_runtime_context_uses_provider_metadata(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt")
        assert context.provider_name == "claude"
        assert context.worktree_path == "/tmp/wt"
        assert context.observation_mode == ObservationMode.HOOKS
        assert "LAZYAGENT_CLAUDE_HOOK_LOG" in context.env_overrides
        assert "CLAUDE_CONFIG_DIR" not in context.env_overrides
        assert "hook_log_path" in context.metadata
        assert "settings_path" in context.metadata

    def test_codex_returns_app_server_observer(self):
        observer = get_agent_provider("codex").create_observer("/tmp/wt")
        assert isinstance(observer, CodexAppServerObserver)

    def test_claude_returns_hooks_observer(self):
        observer = get_agent_provider("claude").create_observer("/tmp/wt")
        assert isinstance(observer, ClaudeHooksObserver)


class TestClaudeHooksSettings:
    def test_settings_file_contains_only_hooks(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt")

        settings_path = context.metadata["settings_path"]
        settings = json.loads(open(settings_path, encoding="utf-8").read())

        # Only hooks, no user config leaking in
        assert list(settings.keys()) == ["hooks"]

        # All expected hook events are present
        assert "Notification" in settings["hooks"]
        assert "Stop" in settings["hooks"]
        assert "PreToolUse" in settings["hooks"]
        assert "PostToolUse" in settings["hooks"]
        assert "TaskCompleted" in settings["hooks"]
        assert "SessionEnd" in settings["hooks"]

    def test_build_command_includes_settings_flag(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt")
        command = provider.build_command("/tmp/wt", runtime_context=context)
        script = shlex.split(command)[2]
        assert "--settings" in script
        assert context.metadata["settings_path"] in script
