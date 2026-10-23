"""The worker preamble (proposal 0013 §7a b, I4): a few fixed lines every
worker lazyagent spawns gets: Claude with --append-system-prompt, Codex
prepended to its instruction; replaced or turned off with ``[agents]
worker_preamble``; the orchestrator keeps its own prompt."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from lazyagent.agent_providers import PROVIDERS
from lazyagent.config import AgentConfig, Config, load_config
from lazyagent.orchestrator_prompt import DEFAULT_ORCHESTRATOR_PROMPT
from lazyagent.worker_prompt import (
    DEFAULT_WORKER_PREAMBLE,
    compose_worker_preamble,
    worker_system_prompt,
)
from tests.engine.fakes import make_engine, no_pty, worktree

FEAT = "/tmp/repo-feat"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # Provider runtime contexts must never touch the real home.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _engine(monkeypatch, provider: str, preamble: str | None = None):
    no_pty(monkeypatch)
    config = Config(agent=AgentConfig(provider=provider, worker_preamble=preamble))
    return make_engine(
        [worktree("/tmp/repo"), worktree(FEAT, "feat", is_main=False)],
        config=config,
        provider=PROVIDERS[provider],
    )


def _words(command: str) -> list[str]:
    """The agent CLI's argv inside the ``bash -c`` wrapper."""
    script = shlex.split(command)[2]
    return shlex.split(script.rpartition(" && exec ")[2])


def _flag(words: list[str], flag: str) -> str | None:
    return words[words.index(flag) + 1] if flag in words else None


def test_the_default_preamble_names_the_tools_and_is_short():
    text = DEFAULT_WORKER_PREAMBLE
    for tool in ("request_decision", "status_report", "notes_append", "decision_withdraw"):
        assert tool in text
    assert len(text.splitlines()) <= 12


def test_worker_preamble_config(tmp_path: Path):
    assert compose_worker_preamble(Config()) == DEFAULT_WORKER_PREAMBLE
    (tmp_path / ".lazyagent.toml").write_text('[agents]\nworker_preamble = "Be brief."\n')
    assert compose_worker_preamble(load_config(tmp_path)) == "Be brief."
    (tmp_path / ".lazyagent.toml").write_text('[agents]\nworker_preamble = ""\n')
    assert compose_worker_preamble(load_config(tmp_path)) is None
    # The older [agent] table works too; [agents] wins.
    (tmp_path / ".lazyagent.toml").write_text(
        '[agent]\nworker_preamble = "old"\n[agents]\nworker_preamble = "new"\n'
    )
    assert compose_worker_preamble(load_config(tmp_path)) == "new"
    (tmp_path / ".lazyagent.toml").write_text('[agent]\nworker_preamble = "old"\n')
    assert compose_worker_preamble(load_config(tmp_path)) == "old"


def test_a_provider_with_no_flag_gets_it_only_with_an_instruction():
    config = Config()
    assert worker_system_prompt(config, PROVIDERS["claude"], None) == DEFAULT_WORKER_PREAMBLE
    assert worker_system_prompt(config, PROVIDERS["codex"], "fix it") == DEFAULT_WORKER_PREAMBLE
    # It would become the agent's first prompt.
    assert worker_system_prompt(config, PROVIDERS["codex"], None) is None


@pytest.mark.asyncio
async def test_claude_gets_it_as_an_appended_system_prompt(monkeypatch):
    engine = _engine(monkeypatch, "claude")
    info = await engine.spawn_agent(FEAT, "fix the login")
    words = _words(engine.get_session(FEAT, info.agent_id).command)
    assert _flag(words, "--append-system-prompt") == DEFAULT_WORKER_PREAMBLE
    assert words[-1] != DEFAULT_WORKER_PREAMBLE and "fix the login" in words


@pytest.mark.asyncio
async def test_codex_gets_it_before_its_instruction(monkeypatch):
    engine = _engine(monkeypatch, "codex")
    info = await engine.spawn_agent(FEAT, "fix the login")
    words = _words(engine.get_session(FEAT, info.agent_id).command)
    assert words[-1] == DEFAULT_WORKER_PREAMBLE + "\n\nfix the login"
    assert "--append-system-prompt" not in words

    bare = await engine.spawn_agent(FEAT)
    words = _words(engine.get_session(FEAT, bare.agent_id).command)
    assert not any(DEFAULT_WORKER_PREAMBLE in w for w in words)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_an_empty_preamble_turns_it_off(monkeypatch, provider):
    engine = _engine(monkeypatch, provider, preamble="")
    info = await engine.spawn_agent(FEAT, "fix the login")
    words = _words(engine.get_session(FEAT, info.agent_id).command)
    assert "--append-system-prompt" not in words
    assert "fix the login" in words
    assert not any(DEFAULT_WORKER_PREAMBLE in w for w in words)


@pytest.mark.asyncio
async def test_a_custom_preamble_replaces_the_default(monkeypatch):
    engine = _engine(monkeypatch, "claude", preamble="Report with status_report.")
    info = await engine.spawn_agent(FEAT)
    words = _words(engine.get_session(FEAT, info.agent_id).command)
    assert _flag(words, "--append-system-prompt") == "Report with status_report."


@pytest.mark.asyncio
async def test_the_orchestrator_keeps_its_own_prompt(monkeypatch):
    engine = _engine(monkeypatch, "claude")
    info = await engine.spawn_agent("", kind="orchestrator")
    prompt = _flag(_words(engine.get_session(info.worktree_path, "").command),
                   "--append-system-prompt")
    assert prompt is not None and prompt.startswith(DEFAULT_ORCHESTRATOR_PROMPT[:40])
    assert DEFAULT_WORKER_PREAMBLE not in prompt
    assert "request_decision" in DEFAULT_ORCHESTRATOR_PROMPT
