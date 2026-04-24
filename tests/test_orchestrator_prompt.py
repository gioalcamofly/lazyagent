from __future__ import annotations

from pathlib import Path

from lazyagent.config import Config, OrchestratorConfig
from lazyagent.orchestrator_prompt import (
    DEFAULT_ORCHESTRATOR_PROMPT,
    DEFAULT_USER_PROMPT_FILE,
    compose_orchestrator_prompt,
)


class TestDefaultPrompt:
    def test_contains_identity(self):
        assert "orchestrator" in DEFAULT_ORCHESTRATOR_PROMPT.lower()

    def test_contains_mcp_tools(self):
        for tool in [
            "list_worktrees",
            "create_worktree",
            "remove_worktree",
            "spawn_agent",
            "stop_agent",
            "get_agent_status",
            "read_agent_output",
        ]:
            assert tool in DEFAULT_ORCHESTRATOR_PROMPT

    def test_contains_workflow_phases(self):
        assert "### 1. Assess" in DEFAULT_ORCHESTRATOR_PROMPT
        assert "### 3. Execute" in DEFAULT_ORCHESTRATOR_PROMPT
        assert "### 4. Monitor" in DEFAULT_ORCHESTRATOR_PROMPT


class TestComposeOrchestratorPrompt:
    def test_default_only(self, tmp_path: Path):
        config = Config()
        result = compose_orchestrator_prompt(config, tmp_path)
        assert result == DEFAULT_ORCHESTRATOR_PROMPT

    def test_with_user_prompt_file(self, tmp_path: Path):
        (tmp_path / DEFAULT_USER_PROMPT_FILE).write_text("Custom user rules here.")
        config = Config()
        result = compose_orchestrator_prompt(config, tmp_path)
        assert DEFAULT_ORCHESTRATOR_PROMPT in result
        assert "Custom user rules here." in result

    def test_with_custom_prompt_file_path(self, tmp_path: Path):
        (tmp_path / "my-prompt.md").write_text("My custom prompt.")
        config = Config(
            orchestrator=OrchestratorConfig(prompt_file="my-prompt.md")
        )
        result = compose_orchestrator_prompt(config, tmp_path)
        assert "My custom prompt." in result

    def test_missing_user_file_returns_default_only(self, tmp_path: Path):
        config = Config()
        result = compose_orchestrator_prompt(config, tmp_path)
        assert result == DEFAULT_ORCHESTRATOR_PROMPT

    def test_with_template(self, tmp_path: Path):
        config = Config(
            orchestrator=OrchestratorConfig(
                agent_instruction_template="/clickup-fix-card {task_id}"
            )
        )
        result = compose_orchestrator_prompt(config, tmp_path)
        assert "/clickup-fix-card {task_id}" in result
        assert "Agent instruction template" in result

    def test_with_template_and_user_file(self, tmp_path: Path):
        (tmp_path / DEFAULT_USER_PROMPT_FILE).write_text("Extra rules.")
        config = Config(
            orchestrator=OrchestratorConfig(
                agent_instruction_template="/fix {id}"
            )
        )
        result = compose_orchestrator_prompt(config, tmp_path)
        # All three layers present
        assert DEFAULT_ORCHESTRATOR_PROMPT in result
        assert "/fix {id}" in result
        assert "Extra rules." in result
        # Template appears before user prompt
        assert result.index("/fix {id}") < result.index("Extra rules.")

    def test_empty_user_file_ignored(self, tmp_path: Path):
        (tmp_path / DEFAULT_USER_PROMPT_FILE).write_text("   \n  ")
        config = Config()
        result = compose_orchestrator_prompt(config, tmp_path)
        assert result == DEFAULT_ORCHESTRATOR_PROMPT
