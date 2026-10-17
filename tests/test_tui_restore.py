"""The TUI offers the agents an earlier session ran (engine-split §6e, Q30):
a toast when it connects to an engine that has some, and R (or the command
palette) restores them through restore_agents. Both backends; real PTYs with
the fake agent."""

from __future__ import annotations

import asyncio
import time

import pytest

from lazyagent.engine.manifest import Entry
from tests.engine.fakes import fake_agent_provider, make_engine, worktree
from tests.tui_backends import settle, tui_app

pytestmark = pytest.mark.asyncio


async def _until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.02)


def _engine(tmp_path, *, restorable: bool):
    repo, feat = tmp_path / "repo", tmp_path / "repo-feat"
    repo.mkdir()
    feat.mkdir()
    engine = make_engine(
        [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
        repo_root=str(repo),
        provider=fake_agent_provider(tmp_path),
    )
    manifest = engine.open_manifest(tmp_path / "manifest.json")
    if restorable:
        entry = Entry(
            id="3f2a9c1d0b7e",
            worktree_path=str(feat),
            agent_id="a4",
            kind="agent",
            provider="fake",
            label="Reviewer",
            ended_at=1.0,
        )
        manifest.restorable = {entry.id: entry}
    return engine, str(feat)


def _toasts(app) -> list[str]:
    seen: list[str] = []
    notify = app.notify

    def record(message, *args, **kwargs):
        seen.append(str(message))
        return notify(message, *args, **kwargs)

    app.notify = record
    return seen


async def test_the_tui_offers_and_restores_earlier_agents(tmp_path, backend_kind):
    engine, feat = _engine(tmp_path, restorable=True)
    app = tui_app(backend_kind, engine=engine, repo_path=engine.repo_path)
    toasts = _toasts(app)
    async with app.run_test() as pilot:
        await _until(lambda: any("can be brought back" in t for t in toasts))
        offer = next(t for t in toasts if "can be brought back" in t)
        assert offer.startswith("1 agent from an earlier session")
        assert sum("can be brought back" in t for t in toasts) == 1

        await pilot.press("ctrl+k")  # out of any terminal
        await pilot.press("R")
        await _until(lambda: engine.get_session(feat, "a4") is not None)
        await _until(lambda: any(t == "Restored 1 agent" for t in toasts))
        await settle(pilot)
        assert "a4" in app.backend.agent_ids(feat)
        assert engine.get_agent(feat, "a4").label == "Reviewer"
        assert engine.list_restorable() == []

        await pilot.press("R")
        await _until(lambda: "Nothing to restore" in toasts)
    await engine.stop()


async def test_no_offer_when_nothing_is_restorable(tmp_path, backend_kind):
    engine, _ = _engine(tmp_path, restorable=False)
    app = tui_app(backend_kind, engine=engine, repo_path=engine.repo_path)
    toasts = _toasts(app)
    async with app.run_test() as pilot:
        await settle(pilot)
        await asyncio.sleep(0.2)
        assert not any("can be brought back" in t for t in toasts)
        titles = [command.title for command in app.get_system_commands(app.screen)]
        assert "Restore agents" in titles
    await engine.stop()
