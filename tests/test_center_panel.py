"""Tests for center_panel command construction logic."""
from __future__ import annotations

import shlex

from lazyagent.agent_providers import SENTINEL_SYSTEM_PROMPT, env_exports, get_agent_provider


def _build_spawn_command(
    worktree_path: str,
    skip_permissions: bool = False,
    agent_provider: str = "claude",
) -> str:
    """Reproduce the command-building logic from WorktreePanel.spawn_agent."""
    return get_agent_provider(agent_provider).build_command(
        worktree_path,
        skip_permissions=skip_permissions,
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

    def test_no_trailing_positional_after_sentinel(self):
        """Command should end with the sentinel, no positional prompt arg."""
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        # The sentinel is the last argument. Find where it ends in the script.
        sentinel_end = "completed your task."
        idx = script.rfind(sentinel_end)
        assert idx != -1, "Sentinel end not found in script"
        after_sentinel = script[idx + len(sentinel_end):]
        # After the sentinel's closing quote, only whitespace/quotes should remain
        stripped = after_sentinel.strip().strip("'\"")
        assert stripped == "", f"Unexpected trailing content after sentinel: {stripped!r}"

    def test_worktree_path_with_spaces(self):
        cmd = _build_spawn_command("/home/user/my worktree")
        argv = shlex.split(cmd)
        assert len(argv) == 3
        assert "/home/user/my worktree" in argv[2]

    def test_sentinel_prompt_with_quotes_preserved(self):
        """The sentinel system prompt contains 'your turn' with quotes."""
        cmd = _build_spawn_command("/tmp/wt")
        script = shlex.split(cmd)[2]
        assert "your turn" in script

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


class TestEnvExports:
    def test_includes_path(self):
        """PATH should be exported."""
        exports = env_exports()
        assert "PATH=" in exports

    def test_skips_term(self):
        """TERM is set by textual-terminal, should not be overridden."""
        exports = env_exports()
        # TERM should not appear as a key (it could appear as substring of another var)
        parts = exports.removeprefix("export ").split()
        keys = [p.split("=")[0] for p in parts]
        assert "TERM" not in keys

    def test_skips_home(self):
        """HOME is set by textual-terminal, should not be overridden."""
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
