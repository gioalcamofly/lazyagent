from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lazyagent.agent_providers import DEFAULT_AGENT_PROVIDER, normalize_provider_name

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

CONFIG_FILENAME = ".lazyagent.toml"


DEFAULT_BRANCH = "master"


@dataclass
class WorktreeConfig:
    """Custom worktree create/remove command templates."""

    create: str | None = None
    remove: str | None = None


@dataclass
class AgentConfig:
    """Configuration for agent process selection."""

    provider: str = DEFAULT_AGENT_PROVIDER


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator agent."""

    prompt_file: str | None = None
    agent_instruction_template: str | None = None


@dataclass
class Config:
    """Application configuration loaded from .lazyagent.toml."""

    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    default_branch: str = DEFAULT_BRANCH

    @property
    def has_custom_create(self) -> bool:
        return self.worktree.create is not None

    @property
    def has_custom_remove(self) -> bool:
        return self.worktree.remove is not None


def load_config(repo_root: str | Path) -> Config:
    """Load configuration from .lazyagent.toml in repo root.

    Returns default Config if file is missing or tomllib is unavailable.
    """
    config_path = Path(repo_root) / CONFIG_FILENAME
    if not config_path.exists():
        return Config()

    if tomllib is None:
        return Config()

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    wt_data = data.get("worktree", {})
    worktree_config = WorktreeConfig(
        create=wt_data.get("create"),
        remove=wt_data.get("remove"),
    )
    agent_data = data.get("agent", {})
    provider = normalize_provider_name(agent_data.get("provider"))
    orch_data = data.get("orchestrator", {})
    orchestrator_config = OrchestratorConfig(
        prompt_file=orch_data.get("prompt_file"),
        agent_instruction_template=orch_data.get("agent_instruction_template"),
    )
    default_branch = data.get("default_branch", DEFAULT_BRANCH)
    return Config(
        worktree=worktree_config,
        agent=AgentConfig(provider=provider),
        orchestrator=orchestrator_config,
        default_branch=default_branch,
    )


def format_command(
    template: str,
    *,
    branch: str = "",
    name: str = "",
    base: str = "",
    path: str = "",
    repo: str = "",
    force: str = "",
    extra: str = "",
) -> str:
    """Expand placeholders and ~ in a command template.

    Placeholders: {branch}, {name}, {base}, {path}, {repo}, {force}, {extra}

    **Security note:** the result is sent verbatim to a shell (``bash -c``
    or pasted into a terminal). Substituted values are NOT shell-quoted —
    template authors are responsible for placement. In particular:

    - Do NOT embed placeholders inside double quotes; a value containing
      ``"`` or ``$`` will break the surrounding quoting and can execute
      arbitrary shell. Single-quote (``'{branch}'``) or use ``{branch}``
      bare and rely on the value not containing whitespace.
    - When values can come from an LLM (via the MCP ``extra`` param or
      a branch name that passed through the orchestrator), prefer
      template designs that don't depend on quoting at all (e.g. pass
      values via env vars: ``BRANCH={branch} ./create-worktree.sh``).

    This is intentional: locking values would break templates that already
    rely on shell expansion (``{path}`` interpolating ``~``, ``{extra}``
    expanding to multiple flags, etc.).
    """
    expanded = os.path.expanduser(template)
    return expanded.format(
        branch=branch,
        name=name,
        base=base,
        path=path,
        repo=repo,
        force=force,
        extra=extra,
    ).strip()
