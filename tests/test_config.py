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
        assert result == "script.sh  "

    def test_no_placeholders(self):
        result = format_command("simple-command", branch="ignored")
        assert result == "simple-command"
