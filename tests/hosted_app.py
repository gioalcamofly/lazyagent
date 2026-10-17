"""A LazyAgent app on a fake git world, for the hosted-mode tests.

Its engine is :func:`tests.engine.fakes.make_engine` with two worktrees,
``repo`` (main) and ``repo-feat``, and the fake agent provider. The real
engine start runs, so the v1 server listens in the test's runtime dir.
``backend_kind`` picks the app's backend (tests/tui_backends.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lazyagent.app import LazyAgent
from lazyagent.protocol.hosted import HOST_ENV
from tests.engine.fakes import fake_agent_provider, make_engine, worktree
from tests.tui_backends import make_backend


def make_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    hosted: bool,
    backend_kind: str = "inprocess",
) -> LazyAgent:
    if hosted:
        monkeypatch.setenv(HOST_ENV, "test-host")
    else:
        monkeypatch.delenv(HOST_ENV, raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    # The worktree shells are real `bash -l`: keep the user's profile out.
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "repo"
    feat = tmp_path / "repo-feat"
    repo.mkdir()
    feat.mkdir()
    engine = make_engine(
        [worktree(str(repo)), worktree(str(feat), "feat", is_main=False)],
        repo_root=str(repo),
        provider=fake_agent_provider(tmp_path),
    )
    app = LazyAgent(repo_path=str(repo), backend=make_backend(backend_kind, engine))
    app.repo, app.feat = str(repo), str(feat)  # type: ignore[attr-defined]
    return app
