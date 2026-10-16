"""MonitoredTerminal re-posts its session's events as Textual messages.

Status detection itself (output, screen scan, hang, exit) is tested on the
session in tests/engine/test_session.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from lazyagent.engine.session import AgentSession
from lazyagent.messages import AgentExited, AgentStatusChanged
from lazyagent.models import AgentStatus, LifecycleConfidence
from lazyagent.widgets.monitored_terminal import MonitoredTerminal

WT_PATH = "/home/user/repo-worktree"


def _make_terminal() -> tuple[MonitoredTerminal, AgentSession]:
    session = AgentSession("unused", worktree_path=WT_PATH, agent_id="a2")
    terminal = MonitoredTerminal(session)
    terminal.post_message = MagicMock()
    return terminal, session


def test_status_change_is_posted_as_a_message():
    terminal, session = _make_terminal()
    session.last_output_time = 12.5
    session._set_status(
        AgentStatus.WAITING, confidence=LifecycleConfidence.HIGH, detail="prompt"
    )
    msg = terminal.post_message.call_args[0][0]
    assert isinstance(msg, AgentStatusChanged)
    assert (msg.worktree_path, msg.agent_id) == (WT_PATH, "a2")
    assert msg.status == AgentStatus.WAITING
    assert msg.confidence == LifecycleConfidence.HIGH
    assert msg.detail == "prompt"
    assert msg.last_output_time == 12.5


def test_exit_is_posted_with_its_code():
    from lazyagent.engine.session import SessionExited

    terminal, session = _make_terminal()
    session._emit(SessionExited(WT_PATH, "a2", 3))
    msg = terminal.post_message.call_args[0][0]
    assert isinstance(msg, AgentExited)
    assert (msg.worktree_path, msg.agent_id, msg.exit_code) == (WT_PATH, "a2", 3)


def test_view_reads_identity_and_status_from_the_session():
    terminal, session = _make_terminal()
    session.status = AgentStatus.RUNNING
    session.last_output_time = 3.0
    assert terminal.worktree_path == WT_PATH
    assert terminal.agent_id == "a2"
    assert terminal.agent_status == AgentStatus.RUNNING
    assert terminal.last_output_time == 3.0
    assert terminal._screen is session.screen


def test_unmount_stops_relaying():
    terminal, session = _make_terminal()
    terminal.on_unmount()
    session._set_status(AgentStatus.WAITING)
    terminal.post_message.assert_not_called()
    assert session._viewers == []
