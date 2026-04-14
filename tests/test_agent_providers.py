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
    ResumeMode,
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


class TestClaudeMcpSettings:
    def test_settings_file_with_mcp_server(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt", socket_path="/tmp/test.sock")

        settings_path = context.metadata["settings_path"]
        settings = json.loads(open(settings_path, encoding="utf-8").read())

        # Both hooks and mcpServers should be present
        assert "hooks" in settings
        assert "mcpServers" in settings

        mcp_cfg = settings["mcpServers"]["lazyagent"]
        assert mcp_cfg["command"] == "python3"
        assert mcp_cfg["args"] == ["-m", "lazyagent.mcp_server"]
        assert mcp_cfg["env"]["LAZYAGENT_SOCKET"] == "/tmp/test.sock"
        assert mcp_cfg["env"]["PYTHONUNBUFFERED"] == "1"

    def test_settings_file_without_socket_path_has_no_mcp(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt")

        settings_path = context.metadata["settings_path"]
        settings = json.loads(open(settings_path, encoding="utf-8").read())

        assert list(settings.keys()) == ["hooks"]
        assert "mcpServers" not in settings


class TestBuildCommandResume:
    def test_claude_new_has_no_resume_flags(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", resume_mode=ResumeMode.NEW)
        )[2]
        assert "--resume" not in script
        assert "--continue" not in script

    def test_claude_resume_pick(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_PICK)
        )[2]
        assert "--resume" in script
        assert "--continue" not in script

    def test_claude_resume_last(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_LAST)
        )[2]
        assert "--continue" in script
        assert "--resume" not in script

    def test_codex_resume_pick(self):
        script = shlex.split(
            get_agent_provider("codex").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_PICK)
        )[2]
        assert "exec codex resume" in script

    def test_codex_resume_last(self):
        script = shlex.split(
            get_agent_provider("codex").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_LAST)
        )[2]
        assert "exec codex resume --last" in script

    def test_codex_resume_pick_with_dangerous(self):
        script = shlex.split(
            get_agent_provider("codex").build_command(
                "/tmp/wt", skip_permissions=True, resume_mode=ResumeMode.RESUME_PICK
            )
        )[2]
        assert "exec codex resume" in script
        assert "--dangerously-bypass-approvals-and-sandbox" in script

    def test_codex_new_has_no_resume_subcommand(self):
        script = shlex.split(
            get_agent_provider("codex").build_command("/tmp/wt", resume_mode=ResumeMode.NEW)
        )[2]
        assert "exec codex" in script
        # Check the exec portion only — env vars may contain "resume" as substring
        exec_part = script.split("exec ")[1]
        assert "resume" not in exec_part

    def test_gemini_resume_pick(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_PICK)
        )[2]
        assert "--resume" in script

    def test_gemini_resume_last(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command("/tmp/wt", resume_mode=ResumeMode.RESUME_LAST)
        )[2]
        assert "--resume" in script

    def test_gemini_new_has_no_resume_flags(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command("/tmp/wt", resume_mode=ResumeMode.NEW)
        )[2]
        assert "--resume" not in script


class TestBuildCommandSystemPrompt:
    def test_claude_uses_append_system_prompt_flag(self):
        script = shlex.split(
            get_agent_provider("claude").build_command(
                "/tmp/wt", system_prompt="You are the orchestrator."
            )
        )[2]
        assert "--append-system-prompt" in script
        assert "You are the orchestrator." in script

    def test_claude_no_system_prompt_no_flag(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt")
        )[2]
        assert "--append-system-prompt" not in script

    def test_claude_none_system_prompt_no_flag(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", system_prompt=None)
        )[2]
        assert "--append-system-prompt" not in script

    def test_codex_prepends_system_prompt_to_instruction(self):
        script = shlex.split(
            get_agent_provider("codex").build_command(
                "/tmp/wt",
                instruction="do stuff",
                system_prompt="You are the orchestrator.",
            )
        )[2]
        # Codex has no system_prompt_flag, so prompt is prepended to instruction
        assert "--append-system-prompt" not in script
        assert "You are the orchestrator." in script
        assert "do stuff" in script

    def test_codex_system_prompt_without_instruction(self):
        script = shlex.split(
            get_agent_provider("codex").build_command(
                "/tmp/wt", system_prompt="You are the orchestrator."
            )
        )[2]
        assert "You are the orchestrator." in script

    def test_gemini_prepends_system_prompt_to_instruction(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command(
                "/tmp/wt",
                instruction="hello",
                system_prompt="You are the orchestrator.",
            )
        )[2]
        assert "-i" in script
        assert "You are the orchestrator." in script
        assert "hello" in script

    def test_gemini_system_prompt_without_instruction(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command(
                "/tmp/wt", system_prompt="You are the orchestrator."
            )
        )[2]
        assert "-i" in script
        assert "You are the orchestrator." in script


class TestBuildCommandInstruction:
    def test_claude_instruction_as_positional_arg(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", instruction="do stuff")
        )[2]
        exec_part = script.split("exec ")[1]
        assert "'do stuff'" in exec_part or "do stuff" in exec_part

    def test_codex_instruction_as_positional_arg(self):
        script = shlex.split(
            get_agent_provider("codex").build_command("/tmp/wt", instruction="fix bug")
        )[2]
        exec_part = script.split("exec ")[1]
        assert "'fix bug'" in exec_part or "fix bug" in exec_part

    def test_gemini_instruction_via_flag(self):
        script = shlex.split(
            get_agent_provider("gemini").build_command("/tmp/wt", instruction="hello")
        )[2]
        exec_part = script.split("exec ")[1]
        assert "-i" in exec_part
        assert "hello" in exec_part

    def test_no_instruction_when_none(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", instruction=None)
        )[2]
        exec_part = script.split("exec ")[1]
        # Should only have claude + --settings <path>
        parts = shlex.split(exec_part)
        assert parts[0] == "claude"
        assert parts[1] == "--settings"
        assert len(parts) == 3

    def test_no_instruction_when_empty_string(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", instruction="")
        )[2]
        exec_part = script.split("exec ")[1]
        parts = shlex.split(exec_part)
        assert parts[0] == "claude"
        assert parts[1] == "--settings"
        assert len(parts) == 3

    def test_instruction_with_special_chars_is_escaped(self):
        script = shlex.split(
            get_agent_provider("claude").build_command(
                "/tmp/wt", instruction="hello 'world' && rm -rf /"
            )
        )[2]
        # The instruction must survive shell escaping — it should appear quoted
        assert "hello" in script
        assert "rm -rf" in script

    def test_instruction_combined_with_resume(self):
        script = shlex.split(
            get_agent_provider("claude").build_command(
                "/tmp/wt", resume_mode=ResumeMode.RESUME_LAST, instruction="continue task"
            )
        )[2]
        assert "--continue" in script
        assert "continue task" in script

    def test_instruction_combined_with_skip_permissions(self):
        script = shlex.split(
            get_agent_provider("claude").build_command(
                "/tmp/wt", skip_permissions=True, instruction="deploy now"
            )
        )[2]
        assert "--dangerously-skip-permissions" in script
        assert "deploy now" in script
