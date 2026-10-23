from __future__ import annotations

import os
from pathlib import Path

from lazyagent.config import Config, WorktreeConfig, format_command, load_config


class TestLoadConfig:
    def test_missing_file_returns_defaults(self, tmp_path: Path):
        config = load_config(tmp_path)
        assert config.worktree.create is None
        assert config.worktree.remove is None
        assert config.agent.provider == "claude"
        assert config.default_branch == "master"

    def test_loads_toml_file(self, tmp_path: Path):
        toml_content = """\
[worktree]
create = "~/tools/worktree-new.sh {branch} {name} {base}"
remove = "~/tools/worktree-remove.sh {name}"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.worktree.create == "~/tools/worktree-new.sh {branch} {name} {base}"
        assert config.worktree.remove == "~/tools/worktree-remove.sh {name}"

    def test_empty_toml_returns_defaults(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text("")
        config = load_config(tmp_path)
        assert config.worktree.create is None
        assert config.worktree.remove is None

    def test_partial_config(self, tmp_path: Path):
        toml_content = """\
[worktree]
create = "my-create-script.sh"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.worktree.create == "my-create-script.sh"
        assert config.worktree.remove is None

    def test_custom_default_branch(self, tmp_path: Path):
        toml_content = 'default_branch = "main"\n'
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.default_branch == "main"

    def test_agent_provider_codex(self, tmp_path: Path):
        toml_content = """\
[agent]
provider = "codex"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.agent.provider == "codex"

    def test_cross_session_inbound_defaults_to_none(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text("[agent]\n")
        assert load_config(tmp_path).agent.cross_session_inbound is None

    def test_cross_session_inbound_accept(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            '[agent]\ncross_session_inbound = "accept"\n'
        )
        assert load_config(tmp_path).agent.cross_session_inbound == "accept"

    def test_cross_session_inbound_unknown_value_is_ignored(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            '[agent]\ncross_session_inbound = "always"\n'
        )
        assert load_config(tmp_path).agent.cross_session_inbound is None

    def test_agent_provider_gemini(self, tmp_path: Path):
        toml_content = """\
[agent]
provider = "gemini"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.agent.provider == "gemini"

    def test_invalid_agent_provider_falls_back_to_default(self, tmp_path: Path):
        toml_content = """\
[agent]
provider = "other"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.agent.provider == "claude"

    def test_default_branch_when_not_set(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text("[worktree]\n")
        config = load_config(tmp_path)
        assert config.default_branch == "master"

    def test_orchestrator_defaults(self, tmp_path: Path):
        config = load_config(tmp_path)
        assert config.orchestrator.prompt_file is None
        assert config.orchestrator.agent_instruction_template is None

    def test_orchestrator_prompt_file(self, tmp_path: Path):
        toml_content = """\
[orchestrator]
prompt_file = "my-prompt.md"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.orchestrator.prompt_file == "my-prompt.md"

    def test_orchestrator_agent_instruction_template(self, tmp_path: Path):
        toml_content = """\
[orchestrator]
agent_instruction_template = "/fix-task {task_id}"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.orchestrator.agent_instruction_template == "/fix-task {task_id}"

    def test_orchestrator_partial_config(self, tmp_path: Path):
        toml_content = """\
[orchestrator]
prompt_file = "custom.md"
"""
        (tmp_path / ".lazyagent.toml").write_text(toml_content)
        config = load_config(tmp_path)
        assert config.orchestrator.prompt_file == "custom.md"
        assert config.orchestrator.agent_instruction_template is None


class TestConfigProperties:
    def test_has_custom_create(self):
        config = Config(worktree=WorktreeConfig(create="cmd"))
        assert config.has_custom_create is True

    def test_has_custom_create_false(self):
        config = Config()
        assert config.has_custom_create is False

    def test_has_custom_remove(self):
        config = Config(worktree=WorktreeConfig(remove="cmd"))
        assert config.has_custom_remove is True

    def test_has_custom_remove_false(self):
        config = Config()
        assert config.has_custom_remove is False


class TestFormatCommand:
    def test_expands_placeholders(self):
        result = format_command(
            "script.sh {branch} {name} {base}",
            branch="feat-x",
            name="repo-feat-x",
            base="main",
        )
        assert result == "script.sh feat-x repo-feat-x main"

    def test_expands_tilde(self):
        result = format_command("~/tools/script.sh {branch}", branch="dev")
        expected = os.path.expanduser("~/tools/script.sh dev")
        assert result == expected

    def test_path_and_repo_placeholders(self):
        result = format_command(
            "cmd --path={path} --repo={repo}",
            path="/home/user/repo-feat",
            repo="/home/user/repo",
        )
        assert result == "cmd --path=/home/user/repo-feat --repo=/home/user/repo"

    def test_empty_placeholders_default_to_empty(self):
        result = format_command("script.sh {branch} {name}")
        assert result == "script.sh"

    def test_no_placeholders(self):
        result = format_command("simple-command", branch="ignored")
        assert result == "simple-command"

    def test_force_placeholder(self):
        result = format_command("script.sh {name} {force}", name="wt", force="--force")
        assert result == "script.sh wt --force"

    def test_force_placeholder_empty(self):
        result = format_command("script.sh {name} {force}", name="wt", force="")
        assert result == "script.sh wt"

    def test_extra_placeholder(self):
        result = format_command(
            "script.sh {branch} {extra}", branch="feat", extra="--no-build"
        )
        assert result == "script.sh feat --no-build"

    def test_extra_placeholder_empty(self):
        result = format_command("script.sh {branch} {extra}", branch="feat")
        assert result == "script.sh feat"

    def test_force_not_in_template_is_ignored(self):
        result = format_command("script.sh {name}", name="wt", force="--force")
        assert result == "script.sh wt"


class TestNotesConfig:
    def test_defaults(self, tmp_path: Path):
        notes = load_config(tmp_path).notes
        assert notes == Config().notes
        assert (notes.enabled, notes.mode, notes.max_bytes, notes.orphan_days) == (
            True,
            "propose",
            8192,
            7,
        )
        assert notes.sections == [
            "Goal",
            "Constraints (don't touch)",
            "Decisions",
            "Doubts",
            "Pending",
        ]

    def test_parses_the_section(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            """\
[notes]
enabled = false
mode = "free"
max_bytes = 4096
orphan_days = 2
sections = ["Scope", " Risks "]
"""
        )
        notes = load_config(tmp_path).notes
        assert (notes.enabled, notes.mode, notes.max_bytes, notes.orphan_days) == (
            False,
            "free",
            4096,
            2,
        )
        assert notes.sections == ["Scope", "Risks"]

    def test_invalid_values_keep_their_defaults(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            """\
[notes]
enabled = "yes"
mode = "loud"
max_bytes = 100
orphan_days = -1
sections = ["Goal", ""]
"""
        )
        assert load_config(tmp_path).notes == Config().notes

    def test_bounds_and_types(self, tmp_path: Path):
        for value, expected in (("1024", 1024), ("65536", 65536), ("65537", 8192), ("true", 8192), ("2048.0", 8192)):
            (tmp_path / ".lazyagent.toml").write_text(f"[notes]\nmax_bytes = {value}\n")
            assert load_config(tmp_path).notes.max_bytes == expected, value
        (tmp_path / ".lazyagent.toml").write_text('notes = "on"\n')
        assert load_config(tmp_path).notes == Config().notes


class TestOrchestratorAndStatusConfig:
    def test_defaults(self, tmp_path: Path):
        config = load_config(tmp_path)
        assert config.orchestrator.max_spawns_per_day == 30
        assert config.orchestrator.max_concurrent_agents == 8
        assert (config.status.stale_days, config.status.stalled_hours) == (14, 24)

    def test_reads_the_caps_and_the_status_ages(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            "[orchestrator]\nmax_spawns_per_day = 3\nmax_concurrent_agents = 0\n"
            "[status]\nstale_days = 7\nstalled_hours = 2\n"
        )
        config = load_config(tmp_path)
        assert config.orchestrator.max_spawns_per_day == 3
        assert config.orchestrator.max_concurrent_agents == 0
        assert (config.status.stale_days, config.status.stalled_hours) == (7, 2)

    def test_invalid_values_keep_the_defaults(self, tmp_path: Path):
        (tmp_path / ".lazyagent.toml").write_text(
            "[orchestrator]\nmax_spawns_per_day = -1\nmax_concurrent_agents = \"many\"\n"
            "[status]\nstale_days = 0\nstalled_hours = true\n"
        )
        config = load_config(tmp_path)
        assert config.orchestrator.max_spawns_per_day == 30
        assert config.orchestrator.max_concurrent_agents == 8
        assert (config.status.stale_days, config.status.stalled_hours) == (14, 24)
