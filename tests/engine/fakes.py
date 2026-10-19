"""An Engine wired to fakes: no git, no gh, no real agent CLI.

``make_engine`` builds an :class:`Engine` whose worktree manager answers from
a :class:`World` and whose config is whatever the test passes. Agents are
started through ``provider``: by default a provider that would run nothing
useful (tests patch ``AgentSession.start``), or :func:`fake_agent_provider`,
which runs tests/engine/fake_agent.py under a real PTY.
"""

from __future__ import annotations

import shlex
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lazyagent.agent_providers import AgentProvider
from lazyagent.config import Config
from lazyagent.worktree_manager import PrDiff
from lazyagent.engine.engine import Engine
from lazyagent.engine.session import AgentSession
from lazyagent.models import GitStatus, WorktreeInfo

FAKE_AGENT = Path(__file__).with_name("fake_agent.py")

REPO = "/tmp/repo"


def worktree(path: str = REPO, branch: str | None = "main", is_main: bool = True) -> WorktreeInfo:
    return WorktreeInfo(path=path, head="abc123", branch=branch, is_main=is_main, is_bare=False)


class World:
    """What the fake git would answer, and what it was asked to do."""

    def __init__(self, worktrees: list[WorktreeInfo] | None = None) -> None:
        self.worktrees = list(worktrees or [])
        self.statuses: dict[str, GitStatus] = {}
        self.created: list[tuple[str, str]] = []
        self.removed: list[tuple[str, bool]] = []
        self.create_error: Exception | None = None
        self.list_error: Exception | None = None

    def manager(self, repo_path: str) -> FakeManager:
        return FakeManager(self, repo_path)


class FakeManager:
    """Stands in for WorktreeManager."""

    def __init__(self, world: World, repo_path: str) -> None:
        self.world = world
        self.repo_path = Path(repo_path)

    def list(self) -> list[WorktreeInfo]:
        if self.world.list_error is not None:
            raise self.world.list_error
        return list(self.world.worktrees)

    def create(self, branch: str, base_branch: str = "main") -> str:
        if self.world.create_error is not None:
            raise self.world.create_error
        self.world.created.append((branch, base_branch))
        path = f"{self.repo_path}-{branch}"
        self.world.worktrees.append(worktree(path, branch, is_main=False))
        return path

    def remove(self, path: str, force: bool = False) -> None:
        self.world.removed.append((path, force))
        self.world.worktrees = [wt for wt in self.world.worktrees if wt.path != path]

    def get_all_git_statuses(self, worktrees):
        return {wt.path: self.world.statuses.get(wt.path, GitStatus()) for wt in worktrees}

    def get_git_status(self, path) -> GitStatus:
        return self.world.statuses.get(path, GitStatus())

    def get_last_commit_subject(self, path) -> str:
        return ""

    def get_diff(self, path, *, base=None, max_bytes=None) -> str:
        """Empty, as a clean worktree; a ``base`` called "no-such-branch"
        has no merge-base (the real manager's error)."""
        if base == "no-such-branch":
            raise ValueError(f"No merge-base between {base!r} and HEAD")
        return ""

    def resolve_base_ref(self, path, branch):
        """The fake world has no branches: nothing to diff against."""
        return None

    def get_pr_diff(self, path, base, *, max_bytes=None):
        self.get_diff(path, base=base)
        return PrDiff("", "", "")

    def is_gh_available(self) -> bool:
        return False


def make_engine(
    worktrees: list[WorktreeInfo] | None = None,
    *,
    repo_root: str = REPO,
    config: Config | None = None,
    provider: AgentProvider | None = None,
    world: World | None = None,
) -> Engine:
    """An engine that already knows ``worktrees`` (no event emitted for
    them, so event seqs start at 1 with whatever the test does)."""
    world = world or World(worktrees)
    engine = Engine(
        repo_root,
        manager_factory=world.manager,
        find_root=lambda: repo_root,
        config_loader=lambda root: config or Config(),
        provider_lookup=(lambda name: provider) if provider is not None else None,
        persist_ids=False,
    )
    engine.world = world  # type: ignore[attr-defined]
    engine.worktree_service.repo_root = repo_root
    engine.worktree_service.worktrees = list(world.worktrees)
    if config is not None:
        engine.config = config
    return engine


def fake_agent_provider(directory: Path) -> AgentProvider:
    """A provider whose executable runs fake_agent.py; its instruction is
    passed as the agent's arguments."""
    shim = directory / "fake-agent"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_AGENT} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return AgentProvider(name="fake", executable=str(shim), dangerous_flag="--paste")


def hooked_fake_agent_provider(directory: Path) -> AgentProvider:
    """:func:`fake_agent_provider` with Claude's runtime context: each spawn
    gets a real hooks temp dir (see :func:`hooks_dir`). The fake agent
    ignores the ``--settings`` / ``--mcp-config`` arguments."""
    fake = fake_agent_provider(directory)
    return AgentProvider(name="claude", executable=fake.executable, dangerous_flag="--paste")


def hooks_dir(session) -> Path:
    """The hooks temp dir an agent session was started with."""
    script = shlex.split(session.command)[2]
    words = shlex.split(script.rpartition(" && exec ")[2])
    return Path(words[words.index("--settings") + 1]).parent


def _fake_start(self) -> None:
    import asyncio

    self._emulator = SimpleNamespace(recv_queue=asyncio.Queue(), pid=0)


def _fake_stop(self) -> None:
    self._stopped = True
    self._emulator = None
    self._observer.cleanup()


def no_pty(monkeypatch) -> None:
    """Make AgentSession start/stop pretend: running, but no process."""
    monkeypatch.setattr(AgentSession, "start", _fake_start)
    monkeypatch.setattr(AgentSession, "stop", _fake_stop)


@contextmanager
def pty_free():
    """:func:`no_pty` as a context manager."""
    with patch.object(AgentSession, "start", _fake_start), patch.object(
        AgentSession, "stop", _fake_stop
    ):
        yield


def add_session(engine: Engine, session, *, label: str = "", kind: str = "agent") -> None:
    """Register an already-built session (or a stand-in) with the engine, as
    a spawn would, without starting anything."""
    from lazyagent.engine.engine import AgentInfo, _Agent

    key, agent_id = session.worktree_path, session.agent_id
    info = AgentInfo(key, agent_id, f"s-{key}-{agent_id}", label, "", kind)
    engine._agents.setdefault(key, {})[agent_id] = _Agent(session, info, lambda: None)
