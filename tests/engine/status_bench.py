"""A projection over N worktrees for the status benchmarks: each with an
agent (running, waiting or done), every other one with an open PR (CI
green, review asked), and one answered brief each (briefs are kept 30
days). Fixed, so two runs time the same work."""

from __future__ import annotations

from unittest.mock import MagicMock

from lazyagent.models import AgentState, AgentStatus, CiCheck, PrInfo
from tests.bench import median_time
from tests.engine.fakes import add_session, make_engine, worktree

NOW = 1_700_000_000.0


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def build(n: int):
    """An engine with ``n`` work items; ``engine.status.clock`` is a
    :class:`Clock` at :data:`NOW`."""
    paths = [f"/tmp/repo-b{i:03d}" for i in range(n)]
    engine = make_engine(
        [worktree("/tmp/repo"), *(worktree(p, f"b{i:03d}", is_main=False)
                                  for i, p in enumerate(paths))]
    )
    engine.status.clock = Clock()
    for i, path in enumerate(paths):
        status = (AgentStatus.RUNNING, AgentStatus.WAITING, AgentStatus.COMPLETED)[i % 3]
        engine.registry.states.setdefault(path, {})["a1"] = AgentState(
            status=status, agent_id="a1", detail="Bash"
        )
        session = MagicMock()
        session.worktree_path, session.agent_id = path, "a1"
        add_session(engine, session)
        if i % 2 == 0:
            engine.worktree_service._pr_infos[path] = PrInfo(
                100 + i, f"Change {i}", "OPEN", [CiCheck("tests", "COMPLETED", "SUCCESS")],
                f"https://example.com/pull/{100 + i}", review_decision="REVIEW_REQUIRED",
                mergeable="MERGEABLE",
            )
        engine.status._commit_times[f"b{i:03d}"] = NOW - 3600 - i
        engine.overlay.decisions[f"d{i:03d}"] = {
            "id": f"d{i:03d}", "key": f"repo:b{i:03d}", "worktree_path": path,
            "agent": "a1", "holder": "user", "state": "answered",
            "created_at": NOW - 7200, "closed_at": NOW - 3600,
        }
    engine.status.flush()
    return engine


def time_compute(engine, runs: int = 21) -> float:
    return median_time(engine.status.compute, runs)
