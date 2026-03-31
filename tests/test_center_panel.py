"""Tests for center_panel command construction logic."""
from __future__ import annotations

import shlex

from lazyagent.agent_providers import ResumeMode, env_exports, get_agent_provider


def _build_spawn_command(
    worktree_path: str,
    skip_permissions: bool = False,
    agent_provider: str = "claude",
    resume_mode: ResumeMode = ResumeMode.NEW,
) -> str:
    """Reproduce the command-building logic from WorktreePanel.spawn_agent."""
    return get_agent_provider(agent_provider).build_command(
        worktree_path,
        skip_permissions=skip_permissions,
        resume_mode=resume_mode,
    )


class TestCommandBuilding:
    def test_shlex_split_produces_three_args(self):
        """bash -c <script> should split into exactly ['bash', '-c', script]."""
        cmd = _build_spawn_command("/home/user/repo")
        argv = shlex.split(cmd)
        assert argv[0] == "bash"
        assert argv[1] == "-c"
        assert len(argv) == 3

    def test_script_contains_env_export(self):
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        assert "export " in script
        # PATH should be among the exported vars
        assert "PATH=" in script

    def test_script_contains_cd(self):
        cmd = _build_spawn_command("/home/user/my-worktree")
        script = shlex.split(cmd)[2]
        assert "cd /home/user/my-worktree" in script or "cd '/home/user/my-worktree'" in script

    def test_script_contains_exec_claude(self):
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        assert "exec claude" in script

    def test_command_ends_with_settings_flag(self):
        """Claude command should end with --settings pointing to hooks config."""
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        assert script.rstrip().endswith(".json"), "Command should end with settings JSON path"
        assert "--settings" in script

    def test_worktree_path_with_spaces(self):
        cmd = _build_spawn_command("/home/user/my worktree")
        argv = shlex.split(cmd)
        assert len(argv) == 3
        assert "/home/user/my worktree" in argv[2]

    def test_settings_flag_points_to_hooks_json(self):
        """The --settings flag should reference a hooks settings JSON file."""
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        assert "hooks-settings.json" in script

    def test_skip_permissions_flag(self):
        cmd = _build_spawn_command("/tmp/wt", skip_permissions=True)
        script = shlex.split(cmd)[2]
        assert "--dangerously-skip-permissions" in script

    def test_no_skip_permissions_by_default(self):
        cmd = _build_spawn_command("/tmp/wt", skip_permissions=False)
        script = shlex.split(cmd)[2]
        assert "--dangerously-skip-permissions" not in script

    def test_codex_provider_uses_codex_command(self):
        cmd = _build_spawn_command("/tmp/wt", agent_provider="codex")
        script = shlex.split(cmd)[2]
        assert "exec codex" in script

    def test_codex_provider_uses_dangerous_flag_when_selected(self):
        cmd = _build_spawn_command("/tmp/wt", skip_permissions=True, agent_provider="codex")
        script = shlex.split(cmd)[2]
        assert "exec codex" in script
        assert "--dangerously-bypass-approvals-and-sandbox" in script
        assert "--dangerously-skip-permissions" not in script

    def test_gemini_provider_uses_gemini_command(self):
        cmd = _build_spawn_command("/tmp/wt", agent_provider="gemini")
        script = shlex.split(cmd)[2]
        assert "exec gemini" in script

    def test_gemini_provider_uses_approval_mode_yolo_when_selected(self):
        cmd = _build_spawn_command("/tmp/wt", skip_permissions=True, agent_provider="gemini")
        script = shlex.split(cmd)[2]
        assert "exec gemini" in script
        assert "--approval-mode=yolo" in script
        assert "--append-system-prompt" not in script
        assert "--yolo" not in script


class TestResumeCommandBuilding:
    def test_claude_resume_pick_includes_resume_flag(self):
        cmd = _build_spawn_command("/tmp/wt", resume_mode=ResumeMode.RESUME_PICK)
        script = shlex.split(cmd)[2]
        assert "--resume" in script

    def test_claude_resume_last_includes_continue_flag(self):
        cmd = _build_spawn_command("/tmp/wt", resume_mode=ResumeMode.RESUME_LAST)
        script = shlex.split(cmd)[2]
        assert "--continue" in script

    def test_codex_resume_pick_uses_subcommand(self):
        cmd = _build_spawn_command("/tmp/wt", agent_provider="codex", resume_mode=ResumeMode.RESUME_PICK)
        script = shlex.split(cmd)[2]
        assert "exec codex resume" in script

    def test_gemini_resume_pick_includes_resume_flag(self):
        cmd = _build_spawn_command("/tmp/wt", agent_provider="gemini", resume_mode=ResumeMode.RESUME_PICK)
        script = shlex.split(cmd)[2]
        assert "--resume" in script

    def test_new_session_has_no_resume_flags(self):
        cmd = _build_spawn_command("/tmp/wt", resume_mode=ResumeMode.NEW)
        script = shlex.split(cmd)[2]
        assert "--resume" not in script
        assert "--continue" not in script
        assert "resume" not in script.split("exec")[1].split("--settings")[0]


class TestEnvExports:
    def test_includes_path(self):
        """PATH should be exported."""
        exports = env_exports()
        assert "PATH=" in exports

    def test_skips_term(self):
        """TERM is set by the PTY emulator, should not be overridden."""
        exports = env_exports()
        # TERM should not appear as a key (it could appear as substring of another var)
        parts = exports.removeprefix("export ").split()
        keys = [p.split("=")[0] for p in parts]
        assert "TERM" not in keys

    def test_skips_home(self):
        """HOME is set by the PTY emulator, should not be overridden."""
        exports = env_exports()
        parts = exports.removeprefix("export ").split()
        keys = [p.split("=")[0] for p in parts]
        assert "HOME" not in keys

    def test_custom_var_included(self, monkeypatch):
        """Custom env vars like API keys should be exported."""
        monkeypatch.setenv("CLICKUP_API_KEY", "test-key-123")
        exports = env_exports()
        assert "CLICKUP_API_KEY=" in exports
        assert "test-key-123" in exports

    def test_values_are_quoted(self, monkeypatch):
        """Values with spaces/special chars should be shell-quoted."""
        monkeypatch.setenv("MY_VAR", "value with spaces")
        exports = env_exports()
        assert "MY_VAR=" in exports
        # shlex.quote wraps in single quotes
        assert "'value with spaces'" in exports
