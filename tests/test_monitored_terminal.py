"""MonitoredTerminal is a view of an AgentSession.

Status detection itself (output, screen scan, hang, exit) is tested on the
session in tests/engine/test_session.py, and how it reaches the app on the
engine in tests/engine/test_engine.py.
"""

from __future__ import annotations

from lazyagent.engine.session import AgentSession
from lazyagent.models import AgentStatus
from lazyagent.widgets.monitored_terminal import MonitoredTerminal

WT_PATH = "/home/user/repo-worktree"


def _make_terminal() -> tuple[MonitoredTerminal, AgentSession]:
    session = AgentSession("unused", worktree_path=WT_PATH, agent_id="a2")
    return MonitoredTerminal(session), session


def test_view_reads_identity_and_status_from_the_session():
    terminal, session = _make_terminal()
    session.status = AgentStatus.RUNNING
    session.last_output_time = 3.0
    assert terminal.worktree_path == WT_PATH
    assert terminal.agent_id == "a2"
    assert terminal.agent_status == AgentStatus.RUNNING
    assert terminal.last_output_time == 3.0
    assert terminal._screen is session.screen


def test_the_view_adds_no_session_listener():
    """Status and exit go to the engine, not through the widget tree."""
    _, session = _make_terminal()
    assert session._listeners == []


def test_unmount_detaches_from_the_output():
    terminal, session = _make_terminal()
    assert len(session._viewers) == 1
    terminal.on_unmount()
    assert session._viewers == []
