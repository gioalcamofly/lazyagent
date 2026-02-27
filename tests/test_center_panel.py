"""Tests for center_panel command construction logic."""
from __future__ import annotations

import os
import shlex

from lazyagent.widgets.center_panel import _SENTINEL_SYSTEM_PROMPT


def _build_spawn_command(
    worktree_path: str,
    skip_permissions: bool = False,
    agent_provider: str = "claude",
    path_val: str = "/usr/local/bin:/usr/bin:/bin",
) -> str:
    """Reproduce the command-building logic from WorktreePanel.spawn_agent."""
    provider = (agent_provider or "claude").strip().lower()
    if provider == "codex":
        parts = ["codex"]
        if skip_permissions:
            parts.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        parts = ["claude"]
        if skip_permissions:
            parts.append("--dangerously-skip-permissions")
        parts.extend(["--append-system-prompt", _SENTINEL_SYSTEM_PROMPT])

    inner_cmd = " ".join(shlex.quote(p) for p in parts)
    script = (
        f"export PATH={shlex.quote(path_val)}"
        f" && cd {shlex.quote(worktree_path)}"
        f" && exec {inner_cmd}"
    )
    return f"bash -c {shlex.quote(script)}"


class TestCommandBuilding:
    def test_shlex_split_produces_three_args(self):
        """bash -c <script> should split into exactly ['bash', '-c', script]."""
        cmd = _build_spawn_command("/home/user/repo")
        argv = shlex.split(cmd)
        assert argv[0] == "bash"
        assert argv[1] == "-c"
        assert len(argv) == 3

    def test_script_contains_path_export(self):
        cmd = _build_spawn_command("/tmp/wt", path_val="/foo:/bar")
        script = shlex.split(cmd)[2]
        assert "export PATH=" in script
        assert "/foo:/bar" in script

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

    def test_path_with_colons_preserved(self):
        """PATH contains colons that must survive quoting."""
        path = "/home/user/.local/bin:/usr/local/bin:/usr/bin:/bin"
        cmd = _build_spawn_command("/tmp/wt", path_val=path)
        script = shlex.split(cmd)[2]
        assert path in script

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
