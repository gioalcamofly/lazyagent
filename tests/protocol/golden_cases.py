"""Today's legacy IPC responses, captured as golden fixtures.

Each case drives ``IpcServer._dispatch`` the way tests/test_ipc.py does and
records ``{case, method, params, response}``. The fixtures in
``fixtures/ipc/`` are what test_golden.py validates against the v1 schemas;
it also re-runs these cases to check the fixtures still match ipc.py.

Regenerate after an intended change to ipc.py:

    uv run python -m tests.protocol.golden_cases
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from lazyagent.ipc import IpcServer
from lazyagent.models import (
    AgentState,
    AgentStatus,
    GitStatus,
    LifecycleConfidence,
    WorktreeInfo,
)
from lazyagent.worktree_manager import WorktreeManagerError
from tests.test_ipc import (
    _app_with_agent,
    _make_app,
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


async def _call(app, method: str, params: dict[str, Any], request_id: str = "req-1"):
    server = IpcServer(app, "/tmp/test.sock")
    response = await server._dispatch(request_id, method, params)
    return method, params, response


def _panel_app(agent_ids=("a1",), terminal=None, **kwargs):
    app = _make_app(**kwargs)
    panel = MagicMock()
    panel.agent_ids = list(agent_ids)
    panel.get_agent.return_value = terminal
    center = MagicMock()
    center.get_panel.return_value = panel
    app.query_one.return_value = center
    return app


# --- list_worktrees -------------------------------------------------------

@case("list_worktrees/empty")
async def _():
    return await _call(_make_app(), "list_worktrees", {})


@case("list_worktrees/agents_and_git_status")
async def _():
    app = _make_app(
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
    return await _call(app, "list_worktrees", {})


# --- create_worktree ------------------------------------------------------

@case("create_worktree/plain")
async def _():
    with patch("lazyagent.ipc.WorktreeManager") as wm:
        wm.return_value.create.return_value = "/tmp/repo-feat"
        return await _call(
            _make_app(), "create_worktree", {"branch": "feat", "base_branch": "main"}
        )


@case("create_worktree/plain_with_ignored_extra")
async def _():
    with patch("lazyagent.ipc.WorktreeManager") as wm:
        wm.return_value.create.return_value = "/tmp/repo-feat"
        return await _call(
            _make_app(), "create_worktree", {"branch": "feat", "extra": "--fast"}
        )


@case("create_worktree/custom_command")
async def _():
    app = _make_app()
    app._config.has_custom_create = True
    app._config.worktree.create = "echo creating {branch}"
    proc = MagicMock()
    proc.communicate = MagicMock(return_value=asyncio.Future())
    proc.communicate.return_value.set_result((b"ok\n", b""))
    proc.returncode = 0

    async def _shell(cmd, **kwargs):
        return proc

    with patch("asyncio.create_subprocess_shell", side_effect=_shell):
        return await _call(app, "create_worktree", {"branch": "feat", "base_branch": "main"})


@case("create_worktree/error_empty_branch")
async def _():
    return await _call(_make_app(), "create_worktree", {"branch": ""})


@case("create_worktree/error_worktree_manager")
async def _():
    with patch("lazyagent.ipc.WorktreeManager") as wm:
        wm.return_value.create.side_effect = WorktreeManagerError("branch exists")
        return await _call(_make_app(), "create_worktree", {"branch": "feat"})


# --- remove_worktree ------------------------------------------------------

@case("remove_worktree/removed")
async def _():
    app = _make_app(worktrees=[_make_worktree("/tmp/repo-feat", "feat", is_main=False)])
    with patch("lazyagent.ipc.WorktreeManager"):
        return await _call(
            app, "remove_worktree", {"worktree_path": "/tmp/repo-feat", "force": True}
        )


@case("remove_worktree/error_main")
async def _():
    app = _make_app(worktrees=[_make_worktree()])
    return await _call(app, "remove_worktree", {"worktree_path": "/tmp/repo"})


# --- spawn_agent ----------------------------------------------------------

@case("spawn_agent/spawned")
async def _():
    app = _make_app(worktrees=[_make_worktree("/tmp/repo-feat", "feat", is_main=False)])
    app.call_later = MagicMock()
    with patch("asyncio.wait_for", return_value="a2"):
        return await _call(
            app,
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
    app = _make_app(worktrees=[_make_worktree()])
    app.call_later = MagicMock()
    with patch("asyncio.wait_for", side_effect=TimeoutError):
        return await _call(app, "spawn_agent", {"worktree_path": "/tmp/repo"})


# --- stop_agent -----------------------------------------------------------

@case("stop_agent/stopped")
async def _():
    app = _panel_app(agent_ids=("a1", "a2"))
    app.call_later = MagicMock()
    with patch("asyncio.wait_for", return_value=None):
        return await _call(
            app, "stop_agent", {"worktree_path": "/tmp/repo", "agent_id": "a2"}
        )


# --- get_agent_status -----------------------------------------------------

@case("get_agent_status/no_agent")
async def _():
    return await _call(_make_app(), "get_agent_status", {"worktree_path": "/tmp/repo"})


@case("get_agent_status/waiting_for_user")
async def _():
    app = _make_app(
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
    return await _call(app, "get_agent_status", {"worktree_path": "/tmp/repo"})


@case("get_agent_status/error_ambiguous_agent")
async def _():
    app = _make_app(
        agent_states={
            "/tmp/repo": {
                "a1": AgentState(status=AgentStatus.RUNNING, agent_id="a1"),
                "a2": AgentState(status=AgentStatus.RUNNING, agent_id="a2"),
            }
        }
    )
    return await _call(app, "get_agent_status", {"worktree_path": "/tmp/repo"})


# --- read_agent_output ----------------------------------------------------

@case("read_agent_output/scrollback_and_screen")
async def _():
    import pyte

    from lazyagent.widgets.scrollable_terminal import ScrollbackScreen

    screen = ScrollbackScreen(40, 4)
    stream = pyte.Stream(screen)
    for i in range(12):
        stream.feed(f"line {i}\r\n")
    terminal = MagicMock()
    terminal._screen = screen
    app = _panel_app(terminal=terminal)
    return await _call(app, "read_agent_output", {"worktree_path": "/tmp/repo", "lines": 6})


# --- send_agent_input -----------------------------------------------------

async def _send(status, params, *, reacts=(), detail=""):
    app, terminal = _app_with_agent(status, detail=detail)
    if reacts:
        terminal.send_input.side_effect = _react(app, *reacts)
    return await _call(
        app, "send_agent_input", {"worktree_path": "/tmp/repo", "text": "go", **params}
    )


@case("send_agent_input/confirmed")
async def _():
    return await _send(AgentStatus.WAITING, {}, reacts=(AgentStatus.RUNNING,))


@case("send_agent_input/unconfirmed")
async def _():
    with patch("lazyagent.ipc._INPUT_CONFIRM_TIMEOUT", 0.0):
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
    return await _call(_make_app(), "list_agents", {"worktree_path": "/tmp/repo"})


@case("list_agents/two_agents")
async def _():
    app = _make_app(
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
    return await _call(app, "list_agents", {"worktree_path": "/tmp/repo"})


# --- list_events ----------------------------------------------------------

@case("list_events/from_now")
async def _():
    app, _ = _app_with_agent(AgentStatus.WAITING)
    return await _call(app, "list_events", {})


@case("list_events/since_with_filter")
async def _():
    app, _ = _app_with_agent(AgentStatus.RUNNING)
    _set_status(app, AgentStatus.WAITING)
    _set_status(app, AgentStatus.RUNNING, path="/tmp/other")
    return await _call(app, "list_events", {"since": 0, "statuses": ["idle"]})


@case("list_events/exited_agent")
async def _():
    app, _ = _app_with_agent(AgentStatus.RUNNING)
    app.agent_events.publish(
        worktree_path="/tmp/repo",
        agent_id="a1",
        status="exited",
        previous_status="running",
        detail="",
    )
    return await _call(app, "list_events", {"since": 1, "agent_id": "a1"})


@case("list_events/error_bad_cursor")
async def _():
    app, _ = _app_with_agent(AgentStatus.RUNNING)
    return await _call(app, "list_events", {"since": -1})


# --- dispatch-level errors ------------------------------------------------

@case("dispatch/error_unknown_method")
async def _():
    return await _call(_make_app(), "nonexistent", {})


@case("dispatch/error_internal")
async def _():
    app = _make_app()
    app.worktrees = None  # iterating it raises TypeError inside the handler
    return await _call(app, "list_worktrees", {})


# --------------------------------------------------------------------------

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
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for stale in FIXTURE_DIR.glob("*.json"):
        stale.unlink()
    for name in CASES:
        fixture_path(name).write_text(dump(await capture(name)))
    print(f"wrote {len(CASES)} fixtures to {FIXTURE_DIR}")


if __name__ == "__main__":
    asyncio.run(_write_all())
