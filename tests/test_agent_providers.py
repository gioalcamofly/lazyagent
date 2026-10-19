from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from lazyagent.agent_observers import (
    ClaudeHooksObserver,
    CodexAppServerObserver,
)
from lazyagent.agent_providers import (
    AGENT_TOKEN_ENV,
    DEFAULT_AGENT_PROVIDER,
    MCP_SERVER_NAME,
    NOTES_PATH_ENV,
    ObservationMode,
    ResumeMode,
    agent_session_name,
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
        assert "UserPromptSubmit" in settings["hooks"]
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
    def test_mcp_server_goes_in_mcp_config_not_settings(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt", socket_path="/tmp/test.sock")

        settings_path = context.metadata["settings_path"]
        settings = json.loads(open(settings_path, encoding="utf-8").read())
        # Claude Code ignores mcpServers in a --settings file.
        assert list(settings.keys()) == ["hooks"]

        mcp_config_path = context.metadata["mcp_config_path"]
        mcp_config = json.loads(open(mcp_config_path, encoding="utf-8").read())
        mcp_cfg = mcp_config["mcpServers"][MCP_SERVER_NAME]
        # sys.executable so the MCP server runs in the same interpreter
        # lazyagent itself is running in (handles pipx/venv installs).
        import sys
        assert mcp_cfg["command"] == sys.executable
        assert mcp_cfg["args"] == ["-m", "lazyagent.mcp_server"]
        assert mcp_cfg["env"]["LAZYAGENT_SOCKET"] == "/tmp/test.sock"
        assert mcp_cfg["env"]["PYTHONUNBUFFERED"] == "1"

    def test_command_passes_mcp_config(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt", socket_path="/tmp/test.sock")

        script = shlex.split(provider.build_command("/tmp/wt", runtime_context=context))[2]

        assert f"--mcp-config {context.metadata['mcp_config_path']}" in script

    def test_server_is_not_named_lazyagent(self):
        # A user-scope "lazyagent" server would shadow the per-agent one.
        assert MCP_SERVER_NAME == "lazyagent_app"

    def test_without_socket_path_has_no_mcp(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt")

        settings_path = context.metadata["settings_path"]
        settings = json.loads(open(settings_path, encoding="utf-8").read())

        assert list(settings.keys()) == ["hooks"]
        assert "mcp_config_path" not in context.metadata
        script = shlex.split(provider.build_command("/tmp/wt", runtime_context=context))[2]
        assert "--mcp-config" not in script


class TestSessionName:
    def test_worktree_folder_and_agent_id(self):
        assert agent_session_name("/repos/smith-PROJ-25382", "a1") == "smith-PROJ-25382-a1"

    def test_without_agent_id(self):
        assert agent_session_name("/repos/app") == "app"

    def test_unsafe_characters_are_replaced(self):
        assert agent_session_name("/repos/my app (copy)", "a2") == "my-app-copy-a2"

    def test_claude_gets_name_flag(self):
        script = shlex.split(
            get_agent_provider("claude").build_command("/tmp/wt", session_name="wt-a1")
        )[2]
        assert "--name wt-a1" in script

    def test_providers_without_flag_ignore_it(self):
        script = shlex.split(
            get_agent_provider("codex").build_command("/tmp/wt", session_name="wt-a1")
        )[2]
        assert "wt-a1" not in script


class TestCrossSessionInbound:
    @staticmethod
    def _settings(**kwargs) -> dict:
        context = get_agent_provider("claude").build_runtime_context("/tmp/wt", **kwargs)
        return json.loads(open(context.metadata["settings_path"], encoding="utf-8").read())

    def test_written_when_set(self):
        assert self._settings(cross_session_inbound="accept")["crossSessionInbound"] == "accept"

    def test_left_alone_by_default(self):
        assert "crossSessionInbound" not in self._settings()


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


def test_a_hosted_tuis_marker_is_not_passed_on():
    """LAZYAGENT_HOST is for the hosted TUI: a lazyagent started inside one
    of its agents or shells (which may outlive the host) is not hosted."""
    from lazyagent.agent_providers import env_exports

    script = env_exports(base={"LAZYAGENT_HOST": "lazyagent-host-1", "KEEP": "1"})
    assert "LAZYAGENT_HOST" not in script
    assert "KEEP" in script


class TestAgentToken:
    """The per-spawn token reaches each provider's lazyagent MCP server env."""

    def _env(self, name: str, **kwargs) -> dict[str, str]:
        context = get_agent_provider(name).build_runtime_context(
            "/tmp/wt", socket_path="/tmp/test.sock", **kwargs
        )
        if name == "claude":
            config = json.loads(Path(context.metadata["mcp_config_path"]).read_text())
        elif name == "gemini":
            home = Path(context.env_overrides["GEMINI_CLI_HOME"])
            config = json.loads((home / ".gemini" / "settings.json").read_text())
        else:
            text = (Path(context.env_overrides["CODEX_HOME"]) / "config.toml").read_text()
            table = text.partition(f"[mcp_servers.{MCP_SERVER_NAME}.env]\n")[2]
            return {
                key: json.loads(value)
                for key, _, value in (
                    line.partition(" = ") for line in table.splitlines() if line
                )
            }
        return config["mcpServers"][MCP_SERVER_NAME]["env"]

    @pytest.mark.parametrize("name", ["claude", "codex", "gemini"])
    def test_the_token_lands_in_the_mcp_env(self, name):
        env = self._env(name, agent_token="tok-123")
        assert env == {
            "PYTHONUNBUFFERED": "1",
            "LAZYAGENT_SOCKET": "/tmp/test.sock",
            AGENT_TOKEN_ENV: "tok-123",
        }

    @pytest.mark.parametrize("name", ["claude", "codex", "gemini"])
    def test_no_token_without_one(self, name):
        assert AGENT_TOKEN_ENV not in self._env(name)

    def test_the_token_stays_out_of_the_agents_own_env(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context(
            "/tmp/wt", socket_path="/tmp/test.sock", agent_token="tok-123"
        )
        assert "tok-123" not in provider.build_command("/tmp/wt", runtime_context=context)


class TestClaudeNotesHook:
    """Claude's notes hook (N3): copied next to log_hook.py, registered on
    SessionStart and as a second UserPromptSubmit command."""

    NOTES = "/state/notes/abc/0123456789ab.md"

    def _settings(self, context) -> dict:
        return json.loads(Path(context.metadata["settings_path"]).read_text())

    def _commands(self, settings: dict, event: str) -> list[str]:
        return [
            hook["command"]
            for group in settings["hooks"][event]
            for hook in group["hooks"]
        ]

    def test_registered_on_session_start_and_user_prompt_submit(self):
        context = get_agent_provider("claude").build_runtime_context(
            "/tmp/wt", notes_path=self.NOTES
        )
        settings = self._settings(context)
        log_hook = context.metadata["hook_script_path"]
        notes_hook = context.metadata["notes_hook_path"]
        assert self._commands(settings, "SessionStart") == [log_hook, notes_hook]
        assert self._commands(settings, "UserPromptSubmit") == [log_hook, notes_hook]
        for event in ("Stop", "PreToolUse", "PostToolUse", "Notification"):
            assert notes_hook not in self._commands(settings, event)
        # Every source: the script filters.
        assert "matcher" not in settings["hooks"]["SessionStart"][0]

    def test_the_script_is_copied_and_executable(self):
        context = get_agent_provider("claude").build_runtime_context(
            "/tmp/wt", notes_path=self.NOTES
        )
        script = Path(context.metadata["notes_hook_path"])
        assert script.parent == Path(context.metadata["temp_dir"])
        assert script.stat().st_mode & 0o100
        source = Path(__file__).parents[1] / "src" / "lazyagent" / "notes_hook.py"
        assert script.read_text() == source.read_text()
        assert script.read_text().startswith("#!/usr/bin/env python3\n")

    def test_the_notes_path_reaches_the_agents_env(self):
        provider = get_agent_provider("claude")
        context = provider.build_runtime_context("/tmp/wt", notes_path=self.NOTES)
        assert context.env_overrides[NOTES_PATH_ENV] == self.NOTES
        script = shlex.split(provider.build_command("/tmp/wt", runtime_context=context))[2]
        assert f"{NOTES_PATH_ENV}={self.NOTES}" in script

    def test_without_notes_only_the_logger_runs(self):
        context = get_agent_provider("claude").build_runtime_context("/tmp/wt")
        settings = self._settings(context)
        log_hook = context.metadata["hook_script_path"]
        # SessionStart is still logged: the conversation id comes earlier.
        assert self._commands(settings, "SessionStart") == [log_hook]
        assert self._commands(settings, "UserPromptSubmit") == [log_hook]
        assert "notes_hook_path" not in context.metadata
        assert NOTES_PATH_ENV not in context.env_overrides
        assert not (Path(context.metadata["temp_dir"]) / "notes_hook.py").exists()

    @pytest.mark.parametrize("name", ["codex", "gemini"])
    def test_other_providers_get_no_notes_path(self, name):
        context = get_agent_provider(name).build_runtime_context(
            "/tmp/wt", notes_path=self.NOTES
        )
        assert NOTES_PATH_ENV not in context.env_overrides
