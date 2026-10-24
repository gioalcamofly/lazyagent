"""Today's legacy IPC responses, captured as golden fixtures.

Each case drives ``IpcServer._dispatch`` over an ``Engine`` built from fakes,
the way tests/test_ipc.py does, and
records ``{case, method, params, response}``. The fixtures in
``fixtures/ipc/`` are what test_golden.py validates against the v1 schemas;
it also re-runs these cases to check the fixtures still match ipc.py.

Regenerate after an intended change to ipc.py:

    uv run python -m tests.protocol.golden_cases
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from lazyagent.ipc import IpcServer
from lazyagent.models import (
    AgentState,
    AgentStatus,
    GitStatus,
    LifecycleConfidence,
    WorktreeInfo,
)
from lazyagent.worktree_manager import WorktreeManagerError
from tests.engine.fakes import add_session, pty_free
from tests.test_ipc import (
    _custom,
    _engine_with_agent,
    _fake_proc,
    _make_engine,
    _make_worktree,
    _react,
    _set_status,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ipc"

# AgentEvent.time is wall clock; fixtures store this instead.
FIXED_TIME = 1700000000.0

Case = Callable[[], Awaitable[tuple[str, dict[str, Any], dict[str, Any]]]]
CASES: dict[str, Case] = {}


def case(name: str) -> Callable[[Case], Case]:
    def register(fn: Case) -> Case:
        CASES[name] = fn
        return fn

    return register


async def _call(engine, method: str, params: dict[str, Any], request_id: str = "req-1"):
    server = IpcServer(engine, "/tmp/test.sock")
    response = await server._dispatch(request_id, method, params)
    return method, params, response


# --- list_worktrees -------------------------------------------------------

@case("list_worktrees/empty")
async def _():
    return await _call(_make_engine(), "list_worktrees", {})


@case("list_worktrees/agents_and_git_status")
async def _():
    engine = _make_engine(
        worktrees=[
            _make_worktree("/tmp/repo", "main", is_main=True),
            _make_worktree("/tmp/repo-feat", "feat", is_main=False),
            WorktreeInfo("/tmp/repo-detached", "def456", None, False, False),
        ],
        agent_states={
            "/tmp/repo-feat": {
                "a1": AgentState(
                    status=AgentStatus.RUNNING,
                    confidence=LifecycleConfidence.HIGH,
                    agent_id="a1",
                    label="Agent 1",
                    session_name="repo-feat-a1",
                    detail="Bash",
                ),
                "a2": AgentState(
                    status=AgentStatus.WAITING_FOR_APPROVAL,
                    agent_id="a2",
                    label="Agent 2",
                ),
            }
        },
        git_statuses={"/tmp/repo": GitStatus(dirty_count=2, ahead=1, behind=3)},
    )
    return await _call(engine, "list_worktrees", {})


# --- create_worktree ------------------------------------------------------

@case("create_worktree/plain")
async def _():
    return await _call(
        _make_engine(), "create_worktree", {"branch": "feat", "base_branch": "main"}
    )


@case("create_worktree/plain_with_ignored_extra")
async def _():
    return await _call(
        _make_engine(), "create_worktree", {"branch": "feat", "extra": "--fast"}
    )


@case("create_worktree/custom_command")
async def _():
    engine = _make_engine(config=_custom(create="echo creating {branch}"))

    async def _shell(cmd, **kwargs):
        return _fake_proc(stdout=b"ok\n")

    with patch("asyncio.create_subprocess_shell", side_effect=_shell):
        return await _call(engine, "create_worktree", {"branch": "feat", "base_branch": "main"})


@case("create_worktree/error_empty_branch")
async def _():
    return await _call(_make_engine(), "create_worktree", {"branch": ""})


@case("create_worktree/error_worktree_manager")
async def _():
    engine = _make_engine()
    engine.world.create_error = WorktreeManagerError("branch exists")
    return await _call(engine, "create_worktree", {"branch": "feat"})


# --- remove_worktree ------------------------------------------------------

@case("remove_worktree/removed")
async def _():
    engine = _make_engine(worktrees=[_make_worktree("/tmp/repo-feat", "feat", is_main=False)])
    return await _call(
        engine, "remove_worktree", {"worktree_path": "/tmp/repo-feat", "force": True}
    )


@case("remove_worktree/error_main")
async def _():
    engine = _make_engine(worktrees=[_make_worktree()])
    return await _call(engine, "remove_worktree", {"worktree_path": "/tmp/repo"})


# --- spawn_agent ----------------------------------------------------------

@case("spawn_agent/spawned")
async def _():
    engine = _make_engine(worktrees=[_make_worktree("/tmp/repo-feat", "feat", is_main=False)])
    # An earlier agent of this worktree already had a1.
    engine.registry.new_agent_id("/tmp/repo-feat")
    with pty_free():
        return await _call(
            engine,
            "spawn_agent",
            {
                "worktree_path": "/tmp/repo-feat",
                "instruction": "fix the bug",
                "skip_permissions": True,
                "label": "fixer",
                "resume_mode": "new",
            },
        )


@case("spawn_agent/error_timeout")
async def _():
    engine = _make_engine(worktrees=[_make_worktree()])

    async def _stuck(*args, **kwargs):
        await asyncio.sleep(10)

    engine.spawn_agent = _stuck
    with patch("lazyagent.engine.methods._OP_TIMEOUT", 0.0):
        return await _call(engine, "spawn_agent", {"worktree_path": "/tmp/repo"})


# --- stop_agent -----------------------------------------------------------

@case("stop_agent/stopped")
async def _():
    engine = _make_engine(worktrees=[_make_worktree()])
    with pty_free():
        await engine.spawn_agent("/tmp/repo")
        await engine.spawn_agent("/tmp/repo")
        return await _call(
            engine, "stop_agent", {"worktree_path": "/tmp/repo", "agent_id": "a2"}
        )


# --- get_agent_status -----------------------------------------------------

@case("get_agent_status/no_agent")
async def _():
    return await _call(_make_engine(), "get_agent_status", {"worktree_path": "/tmp/repo"})


@case("get_agent_status/waiting_for_user")
async def _():
    engine = _make_engine(
        agent_states={
            "/tmp/repo": {
                "a1": AgentState(
                    status=AgentStatus.WAITING_FOR_USER,
                    confidence=LifecycleConfidence.HIGH,
                    detail="needs input",
                    agent_id="a1",
                )
            }
        }
    )
    return await _call(engine, "get_agent_status", {"worktree_path": "/tmp/repo"})


@case("get_agent_status/error_ambiguous_agent")
async def _():
    engine = _make_engine(
        agent_states={
            "/tmp/repo": {
                "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                "a2": AgentState(status=AgentStatus.RUNNING, agent_id="a2"),
            }
        }
    )
    return await _call(engine, "get_agent_status", {"worktree_path": "/tmp/repo"})


# --- read_agent_output ----------------------------------------------------

@case("read_agent_output/scrollback_and_screen")
async def _():
    from lazyagent.engine.session import TerminalSession

    session = TerminalSession("unused", worktree_path="/tmp/repo", agent_id="a1", rows=4, cols=40)
    session._stream.feed("".join(f"line {i}\r\n" for i in range(12)))
    engine = _make_engine()
    add_session(engine, session)
    return await _call(engine, "read_agent_output", {"worktree_path": "/tmp/repo", "lines": 6})


# --- send_agent_input -----------------------------------------------------

async def _send(status, params, *, reacts=(), detail=""):
    engine, session = _engine_with_agent(status, detail=detail)
    if reacts:
        session.send_input.side_effect = _react(engine, *reacts)
    return await _call(
        engine, "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go", **params}
    )


@case("send_agent_input/confirmed")
async def _():
    return await _send(AgentStatus.WAITING, {}, reacts=(AgentStatus.RUNNING,))


@case("send_agent_input/unconfirmed")
async def _():
    with patch("lazyagent.engine.methods._INPUT_CONFIRM_TIMEOUT", 0.0):
        return await _send(AgentStatus.WAITING, {})


@case("send_agent_input/busy")
async def _():
    return await _send(AgentStatus.RUNNING, {})


@case("send_agent_input/answered_dialog")
async def _():
    return await _send(
        AgentStatus.WAITING_FOR_APPROVAL,
        {"text": "1", "agent_id": "a1"},
        reacts=(AgentStatus.RUNNING,),
        detail="needs approval",
    )


@case("send_agent_input/unchecked")
async def _():
    return await _send(AgentStatus.WAITING, {"confirm": False})


# --- list_agents ----------------------------------------------------------

@case("list_agents/empty")
async def _():
    return await _call(_make_engine(), "list_agents", {"worktree_path": "/tmp/repo"})


@case("list_agents/two_agents")
async def _():
    engine = _make_engine(
        agent_states={
            "/tmp/repo": {
                "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1", label="Agent 1"),
                "a2": AgentState(
                    status=AgentStatus.WAITING_FOR_USER,
                    agent_id="a2",
                    label="Agent 2",
                    session_name="repo-a2",
                ),
            }
        }
    )
    return await _call(engine, "list_agents", {"worktree_path": "/tmp/repo"})


# --- list_events ----------------------------------------------------------

@case("list_events/from_now")
async def _():
    engine, _ = _engine_with_agent(AgentStatus.WAITING)
    return await _call(engine, "list_events", {})


@case("list_events/since_with_filter")
async def _():
    engine, _ = _engine_with_agent(AgentStatus.RUNNING)
    _set_status(engine, AgentStatus.WAITING)
    _set_status(engine, AgentStatus.RUNNING, path="/tmp/other")
    return await _call(engine, "list_events", {"since": 0, "statuses": ["idle"]})


@case("list_events/exited_agent")
async def _():
    engine, _ = _engine_with_agent(AgentStatus.RUNNING)
    engine.events.publish(
        worktree_path="/tmp/repo",
        agent_id="a1",
        status="exited",
        previous_status="running",
        detail="",
    )
    return await _call(engine, "list_events", {"since": 1, "agent_id": "a1"})


@case("list_events/error_bad_cursor")
async def _():
    engine, _ = _engine_with_agent(AgentStatus.RUNNING)
    return await _call(engine, "list_events", {"since": -1})


# --- dispatch-level errors ------------------------------------------------

@case("dispatch/error_unknown_method")
async def _():
    return await _call(_make_engine(), "nonexistent", {})


@case("dispatch/error_internal")
async def _():
    engine = _make_engine()
    engine.worktree_service.worktrees = None  # iterating it raises TypeError in the handler
    return await _call(engine, "list_worktrees", {})


# --------------------------------------------------------------------------

# --- restart manifest (PR 9) ---------------------------------------------

def _restorable_engine():
    """An engine whose manifest offers one agent of an earlier engine."""
    from lazyagent.engine.manifest import Entry

    engine = _make_engine(worktrees=[_make_worktree("/tmp/repo-feat", "feat", is_main=False)])
    engine.open_manifest(Path(tempfile.mkdtemp()) / "manifest.json")
    entry = Entry(
        id="3f2a9c1d0b7e",
        worktree_path="/tmp/repo-feat",
        agent_id="a2",
        kind="agent",
        provider="claude",
        label="Reviewer",
        session_name="repo-feat-a2",
        skip_permissions=True,
        conversation_id="5d3c2b1a-0f9e-4d8c-b7a6-112233445566",
        instruction="review the diff",
        started_at=1699990000.0,
        engine_id="0123456789abcdef0123456789abcdef",
        ended_at=1699999000.0,
    )
    engine.manifest.restorable = {entry.id: entry}
    return engine


@case("list_restorable/one")
async def _():
    return await _call(_restorable_engine(), "list_restorable", {})


@case("restore_agents/restored_and_unknown")
async def _():
    engine = _restorable_engine()
    with pty_free():
        return await _call(
            engine, "restore_agents", {"ids": ["3f2a9c1d0b7e", "nope"]}
        )


@case("dismiss_restorable/all")
async def _():
    return await _call(_restorable_engine(), "dismiss_restorable", {})


@case("list_events/with_a_restore")
async def _():
    engine = _restorable_engine()
    cursor = engine.events.cursor
    with pty_free():
        await engine.restore_agents()
    return await _call(engine, "list_events", {"since": cursor})


# --- session notes (proposal 0011) ----------------------------------------

FEAT = "/tmp/repo-feat"


def _notes_engine(*agents: str, max_bytes: int = 8192):
    """An engine whose agents ``agents`` in /tmp/repo-feat have notes
    (``0a1b2c3d4e5<n>``) and tokens (``tok-<agent id>``), on a fixed clock."""
    from lazyagent.engine.notes import NotesStore

    engine = _make_engine(worktrees=[_make_worktree(FEAT, "feat", is_main=False)])
    engine.notes = NotesStore(
        Path(tempfile.mkdtemp()),
        max_bytes=max_bytes,
        clock=lambda: FIXED_TIME,
        listener=engine._on_notes_event,
    )
    for n, agent_id in enumerate(agents, 1):
        session = _session_in(FEAT, agent_id)
        add_session(engine, session)
        meta = engine.notes.create(FEAT, agent_id, notes_id=f"0a1b2c3d4e5{n}")
        engine._notes_ids[(FEAT, agent_id)] = meta.notes_id
        engine._tokens[f"tok-{agent_id}"] = (FEAT, agent_id)
    return engine


def _session_in(path: str, agent_id: str):
    from unittest.mock import AsyncMock, MagicMock

    session = MagicMock()
    session.worktree_path = path
    session.agent_id = agent_id
    session.running = True
    session.exit_code = None
    session.send_input = AsyncMock()
    return session


def _orphan(engine, text: str = "## Decisions\n- use sqlite\n") -> str:
    """Notes of an agent a1 that exited on its own (an orphan)."""
    from lazyagent.engine.notes import ORPHAN

    meta = engine.notes.create(FEAT, "a1", notes_id="0f0e0d0c0b0a")
    engine.notes.put(meta.notes_id, text, 0, "agent")
    engine.notes.mark(meta.notes_id, ORPHAN)
    return meta.notes_id


@case("notes_get/template")
async def _():
    return await _call(
        _notes_engine("a1"), "notes_get", {"worktree_path": FEAT, "agent_id": "a1"}
    )


@case("notes_put/written")
async def _():
    return await _call(
        _notes_engine("a1"),
        "notes_put",
        {"worktree_path": FEAT, "text": "## Goal\n- ship notes\n", "if_rev": 0},
    )


@case("notes_put/conflict")
async def _():
    engine = _notes_engine("a1")
    engine.notes.append("0a1b2c3d4e51", "Decisions", "use sqlite", "agent")
    return await _call(
        engine, "notes_put", {"worktree_path": FEAT, "text": "## Goal\n", "if_rev": 0}
    )


@case("notes_append/by_token")
async def _():
    return await _call(
        _notes_engine("a1", "a2"),
        "notes_append",
        {"section": "Decisions", "text": "use sqlite", "caller": "tok-a2"},
    )


@case("notes_append/too_large")
async def _():
    return await _call(
        _notes_engine("a1", max_bytes=1024),
        "notes_append",
        {"section": "Pending", "text": "x" * 1100, "caller": "tok-a1"},
    )


@case("notes_append/peer_refused")
async def _():
    return await _call(
        _notes_engine("a1", "a2"),
        "notes_append",
        {
            "worktree_path": FEAT,
            "agent_id": "a1",
            "section": "Decisions",
            "text": "use postgres",
            "caller": "tok-a2",
        },
    )


@case("notes_replace_section/written")
async def _():
    return await _call(
        _notes_engine("a1"),
        "notes_replace_section",
        {
            "worktree_path": FEAT,
            "section": "Pending",
            "text": "- [ ] tests\n- [x] schema",
            "if_rev": 0,
        },
    )


@case("notes_list/orphan")
async def _():
    engine = _notes_engine("a2")
    _orphan(engine)
    return await _call(engine, "notes_list", {"worktree_path": FEAT})


@case("notes_adopt/adopted")
async def _():
    engine = _notes_engine("a2")
    notes_id = _orphan(engine)
    return await _call(
        engine,
        "notes_adopt",
        {"notes_id": notes_id, "worktree_path": FEAT, "agent_id": "a2"},
    )


@case("notes_delete/orphan")
async def _():
    engine = _notes_engine("a2")
    return await _call(engine, "notes_delete", {"notes_id": _orphan(engine)})


@case("stop_agent/with_notes")
async def _():
    engine = _notes_engine("a1")
    engine.notes.append("0a1b2c3d4e51", "Decisions", "use sqlite", "agent")
    return await _call(engine, "stop_agent", {"worktree_path": FEAT})


# --- review replies ---------------------------------------------------------


def _review_engine(*agents: str):
    """:func:`_notes_engine`'s agents, and a review store in memory on a
    fixed clock where a1 was sent comment c1a2b3."""
    from lazyagent.engine.reviews import ReviewStore

    engine = _notes_engine(*agents)
    engine.reviews = ReviewStore(clock=lambda: FIXED_TIME, new_id=lambda: "r0a1b2c3d4e5")
    engine.reviews.register(FEAT, "a1", ["c1a2b3"])
    return engine


@case("review_register/registered")
async def _():
    return await _call(
        _review_engine("a1"),
        "review_register",
        {"worktree_path": FEAT, "agent_id": "a1", "comment_ids": ["c4d5e6", "c7f8a9"]},
    )


@case("review_reply/answered")
async def _():
    return await _call(
        _review_engine("a1"),
        "review_reply",
        {"comment_id": "c1a2b3", "text": "Renamed it.", "caller": "tok-a1"},
    )


@case("review_reply/not_sent_to_you")
async def _():
    return await _call(
        _review_engine("a1", "a2"),
        "review_reply",
        {"comment_id": "c1a2b3", "text": "Done.", "caller": "tok-a2"},
    )


@case("review_replies/kept")
async def _():
    engine = _review_engine("a1")
    engine.reviews.reply((FEAT, "a1"), "c1a2b3", "Renamed it.")
    return await _call(engine, "review_replies", {"worktree_path": FEAT})


# --- status_list -------------------------------------------------------------


def _status_engine(*extra):
    """Four work items on a fixed clock: an agent waiting for approval, one
    working, an approved green PR with no agent, and a merged one (plus
    the ``extra`` worktrees)."""
    from lazyagent.models import CiCheck, PrInfo

    engine = _make_engine(
        worktrees=[
            _make_worktree("/tmp/repo", "main", is_main=True),
            _make_worktree("/tmp/repo-login", "login-ttl", is_main=False),
            _make_worktree("/tmp/repo-export", "export-csv", is_main=False),
            _make_worktree("/tmp/repo-docs", "docs-index", is_main=False),
            _make_worktree("/tmp/repo-typo", "fix-typo", is_main=False),
            *extra,
        ],
        agent_states={
            "/tmp/repo-login": {
                "a1": AgentState(
                    status=AgentStatus.WAITING_FOR_APPROVAL, agent_id="a1", detail="Bash"
                )
            },
            "/tmp/repo-export": {
                "a2": AgentState(status=AgentStatus.RUNNING, agent_id="a2", detail="Edit")
            },
        },
    )
    green = [CiCheck("tests", "COMPLETED", "SUCCESS")]
    engine.worktree_service._pr_infos.update(
        {
            "/tmp/repo-docs": PrInfo(
                14, "Index the docs", "OPEN", green, "https://example.com/pull/14",
                review_decision="APPROVED", mergeable="MERGEABLE",
            ),
            "/tmp/repo-typo": PrInfo(9, "Fix a typo", "MERGED", green),
        }
    )
    engine.status.clock = lambda: FIXED_TIME
    return engine


@case("status_list/rows")
async def _():
    return await _call(_status_engine(), "status_list", {})


@case("status_list/include_done")
async def _():
    return await _call(_status_engine(), "status_list", {"include_done": True})


# --- actionables_* -----------------------------------------------------------


@case("actionables_list/now_and_round")
async def _():
    return await _call(_status_engine(), "actionables_list", {})


@case("actionables_list/include_hidden")
async def _():
    engine = _status_engine()
    engine.actionables.mark(
        "Approval:/tmp/repo-login/a1", "park", until="agent_stopped"
    )
    return await _call(engine, "actionables_list", {"include_hidden": True})


@case("actionables_get/approval")
async def _():
    return await _call(
        _status_engine(), "actionables_get", {"id": "Approval:/tmp/repo-login/a1"}
    )


@case("actionables_get/error_unknown")
async def _():
    return await _call(_status_engine(), "actionables_get", {"id": "Approval:/nope/a9"})


@case("actionables_mark/dismiss")
async def _():
    return await _call(
        _status_engine(),
        "actionables_mark",
        {"id": "Approval:/tmp/repo-login/a1", "action": "dismiss"},
    )


@case("actionables_mark/park_until_a_time")
async def _():
    return await _call(
        _status_engine(),
        "actionables_mark",
        {"id": "Approval:/tmp/repo-login/a1", "action": "park", "until": FIXED_TIME + 3600},
    )


@case("actionables_mark/error_bad_until")
async def _():
    return await _call(
        _status_engine(),
        "actionables_mark",
        {"id": "Approval:/tmp/repo-login/a1", "action": "park", "until": "someday"},
    )


# --- status_gesture / orchestrator_release (Pass to orchestrator) -------------


def _claims_engine():
    """The status engine with an orchestrator running (token ``tok-orch``)."""
    from lazyagent.engine.registry import ORCHESTRATOR_KEY

    engine = _status_engine()
    add_session(engine, _session_in(ORCHESTRATOR_KEY, ""), kind="orchestrator")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    return engine


@case("status_gesture/delegate")
async def _():
    return await _call(
        _claims_engine(),
        "status_gesture",
        {"key": "repo:export-csv", "kind": "delegate", "to": "orchestrator", "ttl_s": 3600},
    )


@case("status_gesture/clear")
async def _():
    engine = _claims_engine()
    engine.claims.claim("repo:export-csv")
    return await _call(engine, "status_gesture", {"key": "repo:export-csv", "kind": "clear"})


@case("status_gesture/error_no_orchestrator")
async def _():
    return await _call(
        _status_engine(), "status_gesture", {"key": "repo:export-csv", "kind": "delegate"}
    )


@case("orchestrator_release/returned")
async def _():
    engine = _claims_engine()
    engine.claims.claim("repo:export-csv")
    return await _call(
        engine,
        "orchestrator_release",
        {"key": "repo:export-csv", "outcome": "returned", "note": "needs a product call",
         "caller": "tok-orch"},
    )


@case("orchestrator_release/error_not_the_orchestrator")
async def _():
    engine = _claims_engine()
    engine.claims.claim("repo:export-csv")
    return await _call(
        engine, "orchestrator_release", {"key": "repo:export-csv", "outcome": "done"}
    )


@case("orchestrator_hold/held")
async def _():
    return await _call(
        _claims_engine(),
        "orchestrator_hold",
        {"key": "repo:docs-index", "note": "waiting on the merge", "ttl_s": 3600,
         "caller": "tok-orch"},
    )


@case("orchestrator_hold/error_not_the_orchestrator")
async def _():
    return await _call(
        _claims_engine(), "orchestrator_hold", {"key": "repo:docs-index", "note": "mine"}
    )


@case("orchestrator_release/hold_done")
async def _():
    engine = _claims_engine()
    engine.claims.hold("repo:docs-index", "waiting on the merge")
    return await _call(
        engine,
        "orchestrator_release",
        {"key": "repo:docs-index", "outcome": "done", "note": "merged by the user",
         "caller": "tok-orch"},
    )


@case("status_gesture/park_the_work_item_indefinitely")
async def _():
    return await _call(
        _status_engine(),
        "status_gesture",
        {"key": "repo:login-ttl", "kind": "park", "until": "indefinite", "text": "after the release"},
    )


# --- Gestures, reports, timeline, decision briefs (proposal 0013 I4) ---------


def _worker_engine():
    """The status engine with its working agent a2 (token ``tok-a2``) on
    repo:export-csv."""
    engine = _status_engine()
    add_session(engine, _session_in("/tmp/repo-export", "a2"))
    engine._tokens["tok-a2"] = ("/tmp/repo-export", "a2")
    return engine


BRIEF = {
    "question": "Retries: fixed or exponential backoff?",
    "options": [
        {"key": "A", "label": "Exponential, cap 30 s", "consequence": "slower failure on outages"},
        {"key": "B", "label": "Fixed 2 s, 5 tries", "consequence": "3/30 runs time out"},
    ],
    "recommendation": "A",
    "tried": "fixed 2 s: 3 of 30 runs hit the 10 s limit",
    "evidence": [
        {"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"},
        {"kind": "log", "ref": "job:tests", "lines": "120-134"},
    ],
    "reversible": "yes",
    "impact_if_wrong": "slower failures under load",
    "caller": "tok-a2",
}


async def _brief(engine, **changes):
    from lazyagent.engine.methods import EngineMethods

    return await EngineMethods(engine).request_decision({**BRIEF, **changes})


@case("status_gesture/label")
async def _():
    return await _call(
        _status_engine(),
        "status_gesture",
        {"key": "repo:export-csv", "kind": "label", "ball": "others", "text": "QA"},
    )


@case("status_gesture/park")
async def _():
    return await _call(
        _status_engine(),
        "status_gesture",
        {"key": "repo:export-csv", "kind": "park", "until": "agent_stopped"},
    )


@case("status_gesture/close")
async def _():
    return await _call(
        _status_engine(), "status_gesture", {"key": "repo:docs-index", "kind": "close", "how": "dropped"}
    )


@case("status_gesture/error_label_without_a_ball")
async def _():
    return await _call(
        _status_engine(), "status_gesture", {"key": "repo:export-csv", "kind": "label"}
    )


@case("status_report/needs_user_and_outward")
async def _():
    return await _call(
        _worker_engine(),
        "status_report",
        {"note": "PR up: pick a reviewer", "needs_user": True,
         "outward": {"kind": "pr_create", "ref": "https://example.com/pull/15", "text": "opened"},
         "caller": "tok-a2"},
    )


@case("status_report/error_no_token")
async def _():
    return await _call(_worker_engine(), "status_report", {"note": "x"})


@case("status_seen/item")
async def _():
    return await _call(_status_engine(), "status_seen", {"key": "repo:export-csv"})


@case("status_timeline/entries")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    return await _call(engine, "status_timeline", {"key": "repo:export-csv"})


@case("request_decision/brief")
async def _():
    return await _call(_worker_engine(), "request_decision", dict(BRIEF))


@case("request_decision/no_evidence_is_quick")
async def _():
    return await _call(
        _worker_engine(), "request_decision", {**BRIEF, "evidence": [], "blocking": True}
    )


@case("request_decision/error_over_the_limit")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    await _brief(engine)
    return await _call(engine, "request_decision", dict(BRIEF))


@case("decision_withdraw/withdrawn")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    return await _call(engine, "decision_withdraw", {"id": "d1", "caller": "tok-a2"})


@case("decision_get/open")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    return await _call(engine, "decision_get", {"id": "d1"})


@case("decisions_list/by_item")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    await _brief(engine, evidence=[])
    return await _call(engine, "decisions_list", {"key": "repo:export-csv"})


# --- The orchestrator as first responder (proposal 0013 I4b) -----------------


def _first_responder_engine(presence: str = "away"):
    """The worker engine with an orchestrator (``tok-orch``), a UI
    connected and the user's presence set."""
    from unittest.mock import AsyncMock

    from lazyagent.engine.registry import ORCHESTRATOR_KEY

    engine = _worker_engine()
    add_session(engine, _session_in(ORCHESTRATOR_KEY, ""), kind="orchestrator")
    engine._tokens["tok-orch"] = (ORCHESTRATOR_KEY, "")
    engine.first_responder.presence.connected = lambda: True
    engine.first_responder.presence.set(presence)
    engine.head_sha = AsyncMock(return_value="abc123")
    return engine


async def _answered(engine):
    from lazyagent.engine.methods import EngineMethods

    await _brief(engine)
    await EngineMethods(engine).decision_answer(
        {"id": "d1", "choice": "A", "reason": "3/30 runs time out with fixed",
         "caller": "tok-orch"}
    )
    return engine


@case("decision_answer/by_the_orchestrator")
async def _():
    engine = _first_responder_engine()
    await _brief(engine)
    return await _call(
        engine, "decision_answer",
        {"id": "d1", "choice": "A", "reason": "3/30 runs time out with fixed",
         "caller": "tok-orch"},
    )


@case("decision_answer/by_the_user")
async def _():
    engine = _worker_engine()
    await _brief(engine)
    return await _call(engine, "decision_answer", {"id": "d1", "choice": "B", "comment": "simpler"})


@case("decision_answer/error_outside_the_mandate")
async def _():
    engine = _first_responder_engine("present")
    await _brief(engine)
    return await _call(
        engine, "decision_answer",
        {"id": "d1", "choice": "A", "reason": "r", "caller": "tok-orch"},
    )


@case("decision_escalate/with_a_suggestion")
async def _():
    engine = _first_responder_engine("present")
    await _brief(engine)
    return await _call(
        engine, "decision_escalate",
        {"id": "d1", "note": "a product call", "suggestion": "B", "caller": "tok-orch"},
    )


@case("decision_revert/overridden")
async def _():
    engine = await _answered(_first_responder_engine())
    return await _call(
        engine, "decision_revert", {"id": "d1", "choice": "B", "comment": "keep it simple"}
    )


@case("presence_report/present")
async def _():
    engine = _status_engine()
    engine.first_responder.presence.connected = lambda: True
    engine.status.clock = lambda: FIXED_TIME
    return await _call(engine, "presence_report", {"input_at": FIXED_TIME - 60})


@case("presence_set/away")
async def _():
    engine = _status_engine()
    engine.first_responder.presence.connected = lambda: True
    engine.status.clock = lambda: FIXED_TIME
    return await _call(engine, "presence_set", {"mode": "away"})


def _inbox_engine():
    """The status engine whose repo root is a fresh directory (the same
    name every run) holding a commented .lazyagent.toml."""
    import shutil
    import tempfile as _tempfile

    root = Path(_tempfile.gettempdir()) / "golden-inbox" / "repo"
    shutil.rmtree(root.parent, ignore_errors=True)
    root.mkdir(parents=True)
    (root / ".lazyagent.toml").write_text(
        "# repo settings\n[inbox]\ndeep_max = 5  # shown on the zone\n"
    )
    engine = _status_engine()
    engine.worktree_service.repo_root = str(root)
    return engine, root.parent


@case("inbox_config_get/defaults")
async def _():
    return await _call(_status_engine(), "inbox_config_get", {})


@case("inbox_config_set/written")
async def _():
    engine, root = _inbox_engine()
    try:
        return await _call(
            engine, "inbox_config_set",
            {"auto_delegate": ["conflict_with_base"], "auto_delegate_target": "owner"},
        )
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# --- work_items_* (external feeders, I6) --------------------------------------


@case("work_items_upsert/linked_by_ticket")
async def _():
    engine = _status_engine(_make_worktree("/tmp/repo-shop", "SHOP-12-checkout", is_main=False))
    return await _call(
        engine,
        "work_items_upsert",
        {"url": "https://tracker.example.com/browse/SHOP-12", "title": "Checkout flow",
         "source": "planner", "priority": "high",
         "project": {"id": "shop", "title": "Shop", "policy": {"continue_while_review": True}},
         "depends_on": ["https://tracker.example.com/browse/SHOP-11"]},
    )


@case("work_items_upsert/not_started_waiting")
async def _():
    return await _call(
        _status_engine(),
        "work_items_upsert",
        {"url": "https://tracker.example.com/browse/SHOP-20", "title": "Refunds",
         "source": "board", "waiting": {"reason": "design review", "until": "indefinite"}},
    )


@case("work_items_upsert/error_bad_source")
async def _():
    return await _call(
        _status_engine(), "work_items_upsert",
        {"url": "https://tracker.example.com/browse/SHOP-1", "title": "x", "source": "my board"},
    )


@case("work_items_close/closed")
async def _():
    engine = _status_engine()
    engine.work_items.upsert(
        {"url": "https://tracker.example.com/browse/SHOP-30", "title": "Coupons",
         "source": "planner"}, FIXED_TIME,
    )
    return await _call(
        engine, "work_items_close", {"url": "https://tracker.example.com/browse/SHOP-30"}
    )


def normalize(value: Any) -> Any:
    """Replace wall-clock event times so fixtures are reproducible."""
    if isinstance(value, dict):
        return {
            k: (FIXED_TIME if k == "time" and isinstance(v, float) else normalize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalize(v) for v in value]
    return value


async def capture(name: str) -> dict[str, Any]:
    method, params, response = await CASES[name]()
    return normalize({"case": name, "method": method, "params": params, "response": response})


def fixture_path(name: str) -> Path:
    return FIXTURE_DIR / (name.replace("/", "__") + ".json")


def load_fixtures() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(FIXTURE_DIR.glob("*.json"))]


def dump(fixture: dict[str, Any]) -> str:
    return json.dumps(fixture, indent=2, sort_keys=True) + "\n"


async def _write_all() -> None:
    # As the test suite's conftest: the engines here must not keep notes (or
    # anything else) in the real state dir.
    os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="golden-state-")
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="golden-config-")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.json"):
        stale.unlink()
    for name in CASES:
        fixture_path(name).write_text(dump(await capture(name)))
    print(f"wrote {len(CASES)} fixtures to {FIXTURE_DIR}")


if __name__ == "__main__":
    asyncio.run(_write_all())
