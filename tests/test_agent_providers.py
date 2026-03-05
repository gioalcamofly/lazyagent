from __future__ import annotations

import shlex

from lazyagent.agent_providers import (
    DEFAULT_AGENT_PROVIDER,
    SENTINEL_SYSTEM_PROMPT,
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
    def test_claude_supports_system_prompt(self):
        provider = get_agent_provider("claude")
        assert provider.executable == "claude"
        assert provider.supports_append_system_prompt is True

    def test_codex_uses_own_dangerous_flag(self):
        provider = get_agent_provider("codex")
        assert provider.executable == "codex"
        assert (
            provider.dangerous_flag
            == "--dangerously-bypass-approvals-and-sandbox"
        )

    def test_gemini_uses_approval_mode_yolo(self):
        provider = get_agent_provider("gemini")
        assert provider.executable == "gemini"
        assert provider.dangerous_flag == "--approval-mode=yolo"
        assert provider.supports_append_system_prompt is False

    def test_invalid_provider_returns_default_provider(self):
        provider = get_agent_provider("other")
        assert provider.name == DEFAULT_AGENT_PROVIDER


class TestBuildCommand:
    def test_claude_command_appends_sentinel_prompt(self):
        command = get_agent_provider("claude").build_command("/tmp/wt")
        script = shlex.split(command)[2]
        assert "--append-system-prompt" in script
        # The sentinel prompt contains single quotes which get shell-escaped
        # by shlex.quote(), so check the inner args after splitting.
        inner_args = shlex.split(script.split("exec ", 1)[1])
        assert SENTINEL_SYSTEM_PROMPT in inner_args

    def test_codex_command_does_not_append_sentinel_prompt(self):
        command = get_agent_provider("codex").build_command("/tmp/wt")
        script = shlex.split(command)[2]
        assert "--append-system-prompt" not in script
        assert SENTINEL_SYSTEM_PROMPT not in script

    def test_gemini_command_uses_yolo_flag_when_requested(self):
        command = get_agent_provider("gemini").build_command(
            "/tmp/wt",
            skip_permissions=True,
        )
        script = shlex.split(command)[2]
        assert "exec gemini" in script
        assert "--approval-mode=yolo" in script

    def test_gemini_command_does_not_append_sentinel_prompt(self):
        command = get_agent_provider("gemini").build_command("/tmp/wt")
        script = shlex.split(command)[2]
        assert "--append-system-prompt" not in script
        assert SENTINEL_SYSTEM_PROMPT not in script
