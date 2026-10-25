"""``lazyagent engine {start,stop,restart,status,logs,restore} [repo]``: run or manage the
repo's engine (docs/design/engine-split.md §5d).

- ``start``: start a detached engine unless one already serves the repo;
  ``--foreground`` runs it in this terminal instead (logs on stderr).
- ``stop``: ask the engine to shut down (v1 ``shutdown``), which stops its
  agents and shells, and wait for the process to exit. ``--if-idle`` only
  stops an engine that runs no agent.
- ``restart``: stop the engine and start a new one (new code after an
  update); ``--restore`` brings back exactly the agents it stopped, each
  into its conversation, the orchestrator as the orchestrator. Older
  restorable entries stay offered.
- ``status``: whether an engine serves the repo, and what it runs. Exit
  status 3 when none does.
- ``logs``: the end of a detached engine's log; ``-f`` follows it.
- ``restore``: the agents an earlier engine ran when it ended (restart
  manifest): list them, bring them back (each resuming its conversation),
  or stop offering them.

Only ``start --foreground`` loads the engine itself; the rest talk to it
over its socket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from lazyagent import __version__
from lazyagent.engine import launcher
from lazyagent.engine.runtime import log_path

NOT_RUNNING = 3
_STOP_TIMEOUT = 15.0


def _print(message: str) -> None:
    print(f"lazyagent engine: {message}", flush=True)


def _error(message: str) -> None:
    print(f"lazyagent engine: {message}", file=sys.stderr, flush=True)


async def _request(socket: Path, method: str, **params: Any) -> Any:
    from lazyagent.engine.client import Client

    client = await Client.connect(socket, client="cli", client_version=__version__)
    try:
        return await client.request(method, **params)
    finally:
        await client.close()


def _describe(info: dict[str, Any]) -> str:
    kind = "detached" if info.get("detached") else "inside a lazyagent TUI"
    return f"pid {info.get('pid')}, {kind}"


def cmd_start(args: argparse.Namespace, root: str) -> int:
    if args.foreground:
        from lazyagent.engine.headless import main as headless_main

        return headless_main([root])
    info = launcher.info(root)
    if info is not None:
        _print(f"already running for {root} ({_describe(info)})")
        return 0
    try:
        socket, launch = launcher.ensure(root, timeout=args.timeout)
    except launcher.EngineStartError as e:
        _error(str(e))
        return 1
    info = launcher.info(root) or {}
    if launch is not None and info.get("pid") == launch.pid:
        _print(f"started for {root} (pid {launch.pid})")
    else:  # another start won the race
        _print(f"already running for {root} ({_describe(info)})")
    _print(f"socket {socket}")
    _print(f"log {log_path(root)}")
    return 0


def cmd_stop(args: argparse.Namespace, root: str) -> int:
    from lazyagent.engine.client import RequestError

    socket = launcher.find(root)
    info = launcher.info(root) if socket is not None else None
    if socket is None or info is None:
        _print(f"no engine running for {root}")
        return 0
    try:
        result = asyncio.run(
            _request(socket, "shutdown", stop_agents=not args.if_idle)
        )
    except RequestError as e:
        _error(e.message)
        return 1
    except (OSError, ConnectionError) as e:
        _error(f"could not reach the engine: {e}")
        return 1
    pid = info.get("pid")
    deadline = time.monotonic() + args.timeout
    while isinstance(pid, int) and _alive(pid):
        if time.monotonic() >= deadline:
            _error(f"pid {pid} is still running after {args.timeout:g}s")
            return 1
        time.sleep(0.02)
    agents = result["agents"]
    _print(
        f"stopped (pid {pid}"
        + (f", {agents} agent{'s' if agents != 1 else ''} stopped" if agents else "")
        + ")"
    )
    return 0


def _wait_gone(pid: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while isinstance(pid, int) and _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


async def _restorable_ids(socket: Path) -> set[str]:
    from lazyagent.engine.client import RequestError

    try:
        agents = (await _request(socket, "list_restorable"))["agents"]
    except RequestError:  # an engine without a manifest
        return set()
    return {a["id"] for a in agents}


def cmd_restart(args: argparse.Namespace, root: str) -> int:
    from lazyagent.engine.client import RequestError

    def step(name: str, detail: str = "") -> None:
        if args.json:
            print(json.dumps({"step": name, "detail": detail}), flush=True)
        else:
            _print(f"{name}{': ' + detail if detail else ''}")

    result: dict[str, Any] = {
        "stopped": 0,
        "old_pid": None,
        "pid": None,
        "restored": [],
        "failed": [],
        "left": 0,
    }

    def finish(code: int, error: str | None = None) -> int:
        result["error"] = error
        if args.json:
            print(json.dumps({"result": result}), flush=True)
        elif error:
            _error(error)
        return code

    socket = launcher.find(root)
    info = launcher.info(root) if socket is not None else None
    before: set[str] = set()
    if socket is not None and info is not None:
        if not info.get("detached"):
            return finish(
                1,
                f"the engine runs inside a lazyagent TUI (pid {info.get('pid')}); "
                "quit that TUI to restart it",
            )
        result["old_pid"] = info.get("pid")
        try:
            before = asyncio.run(_restorable_ids(socket))
            step("stopping", f"pid {info.get('pid')}")
            reply = asyncio.run(_request(socket, "shutdown", stop_agents=True))
        except RequestError as e:
            return finish(1, e.message)
        except (OSError, ConnectionError) as e:
            return finish(1, f"could not reach the engine: {e}")
        result["stopped"] = reply["agents"]
        if not _wait_gone(info.get("pid"), args.timeout):
            return finish(1, f"pid {info.get('pid')} is still running after {args.timeout:g}s")
    step("starting")
    try:
        socket, _ = launcher.ensure(root, timeout=launcher.START_TIMEOUT)
    except launcher.EngineStartError as e:
        return finish(1, str(e))
    result["pid"] = (launcher.info(root) or {}).get("pid")
    step("started", f"pid {result['pid']}")
    try:
        after = asyncio.run(_request(socket, "list_restorable"))["agents"]
    except RequestError:
        after = []
    # Only what this restart stopped: with no engine running, nothing.
    ids = [a["id"] for a in after if a["id"] not in before] if result["old_pid"] else []
    if args.restore and ids:
        step("restoring", f"{len(ids)} agent(s)")
        try:
            restored = asyncio.run(
                _request(socket, "restore_agents", ids=ids, env=dict(os.environ))
            )
        except RequestError as e:
            return finish(1, e.message)
        result["restored"] = restored["restored"]
        result["failed"] = restored["failed"]
        if not args.json:
            for agent in restored["restored"]:
                if agent["kind"] == "orchestrator":
                    what = "the orchestrator"
                else:
                    what = f"{agent['agent_id']} in {agent['worktree_path']}"
                _print(f"restored {what} ({agent['resumed']})")
            for failure in restored["failed"]:
                _error(f"could not restore {failure['id']}: {failure['reason']}")
    result["left"] = len(after) - len(result["restored"])
    if not args.json:
        _print(
            f"restarted (pid {result['pid']}"
            + (f", {result['stopped']} agent(s) stopped" if result["stopped"] else "")
            + ")"
        )
        if result["left"]:
            _print(f"{result['left']} agent(s) to restore: lazyagent engine restore")
    return finish(1 if result["failed"] else 0)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cmd_status(args: argparse.Namespace, root: str) -> int:
    socket = launcher.find(root)
    info = launcher.info(root) if socket is not None else None
    if socket is None or info is None:
        if args.json:
            print(json.dumps({"running": False, "repo_root": root}))
        else:
            _print(f"not running for {root}")
        return NOT_RUNNING
    try:
        state, restorable = asyncio.run(_status_requests(socket))
    except Exception as e:  # noqa: BLE001 (reported, whatever it is)
        _error(f"engine pid {info.get('pid')} does not answer: {e}")
        return 1
    agents = [
        {"worktree_path": wt["path"], **agent}
        for wt in state["worktrees"]
        for agent in wt.get("agents", [])
    ] + [
        {"worktree_path": None, **agent}
        for agent in state["orchestrator"]["agents"]
    ]
    started = info.get("started_at")
    uptime = time.time() - started if isinstance(started, int | float) else None
    status = {
        "running": True,
        "repo_root": root,
        "pid": info.get("pid"),
        "detached": bool(info.get("detached")),
        "version": info.get("version"),
        "engine_id": info.get("engine_id"),
        "socket": str(socket),
        "uptime": round(uptime) if uptime is not None else None,
        "log": str(log_path(root)) if info.get("detached") else None,
        "worktrees": len(state["worktrees"]),
        "agents": [
            {
                "worktree_path": a["worktree_path"],
                "agent_id": a.get("agent_id"),
                "status": a.get("status"),
            }
            for a in agents
        ],
        "restorable": restorable,
    }
    if args.json:
        print(json.dumps(status))
        return 0
    _print(f"running for {root} ({_describe(info)}, version {status['version']})")
    if status["uptime"] is not None:
        _print(f"up {_duration(status['uptime'])}")
    _print(f"socket {socket}")
    if status["log"]:
        _print(f"log {status['log']}")
    _print(f"{status['worktrees']} worktrees, {len(agents)} agents")
    for agent in status["agents"]:
        where = agent["worktree_path"] or "orchestrator"
        _print(f"  {agent['agent_id'] or '(orchestrator)'} {agent['status']} {where}")
    if restorable:
        _print(f"{restorable} agent(s) to restore: lazyagent engine restore")
    return 0


async def _status_requests(socket: Path) -> tuple[dict[str, Any], int | None]:
    from lazyagent.engine.client import Client, RequestError

    client = await Client.connect(socket, client="cli", client_version=__version__)
    try:
        state = await client.request("get_sidebar_state")
        try:
            restorable = len((await client.request("list_restorable"))["agents"])
        except RequestError:
            restorable = None  # no manifest (or an older engine)
        return state, restorable
    finally:
        await client.close()


def _duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _restorable_line(agent: dict[str, Any]) -> str:
    where = (
        "orchestrator" if agent["kind"] == "orchestrator" else agent["worktree_path"]
    )
    resume = "conversation" if agent.get("conversation_id") else "last"
    name = agent["agent_id"] or "(orchestrator)"
    return f"  {agent['id']}  {name}  {where}  {agent['provider']}, resumes {resume}"


def cmd_restore(args: argparse.Namespace, root: str) -> int:
    from lazyagent.engine.client import RequestError

    socket = launcher.find(root)
    if socket is None:
        _error(f"no engine running for {root} (start one: lazyagent engine start)")
        return NOT_RUNNING
    ids = args.ids or None
    try:
        if args.dismiss:
            if ids is None and not args.all:
                _error("name the ids to dismiss, or pass --all")
                return 2
            result = asyncio.run(_request(socket, "dismiss_restorable", ids=ids))
            _print(f"dismissed {len(result['dismissed'])}")
            return 0
        if ids is None and not args.all:
            agents = asyncio.run(_request(socket, "list_restorable"))["agents"]
            if args.json:
                print(json.dumps(agents))
                return 0
            if not agents:
                _print("nothing to restore")
                return 0
            _print(f"{len(agents)} agent(s) to restore (restore --all, or --id ID)")
            for agent in agents:
                print(_restorable_line(agent))
            return 0
        result = asyncio.run(
            _request(socket, "restore_agents", ids=ids, env=dict(os.environ))
        )
    except RequestError as e:
        _error(e.message)
        return 1
    if args.json:
        print(json.dumps(result))
    else:
        for agent in result["restored"]:
            if agent["kind"] == "orchestrator":
                what = "the orchestrator"
            else:
                what = f"{agent['agent_id']} in {agent['worktree_path']}"
            _print(f"restored {what} ({agent['resumed']})")
        for failure in result["failed"]:
            _error(f"could not restore {failure['id']}: {failure['reason']}")
    return 1 if result["failed"] else 0


def cmd_logs(args: argparse.Namespace, root: str) -> int:
    path = log_path(root)
    if args.path:
        print(path)
        return 0
    if not path.exists():
        _error(f"no log yet at {path}")
        return 1
    text = launcher.tail(path, args.lines)
    if text:
        print(text, flush=True)
    if not args.follow:
        return 0
    try:
        _follow(path)
    except KeyboardInterrupt:
        pass
    return 0


def _follow(path: Path) -> None:
    """Print what is appended to ``path``, across rotations."""
    f = open(path, "rb")  # noqa: SIM115 (reopened on rotation)
    f.seek(0, os.SEEK_END)
    inode = os.fstat(f.fileno()).st_ino
    try:
        while True:
            data = f.read()
            if data:
                sys.stdout.write(data.decode(errors="replace"))
                sys.stdout.flush()
                continue
            time.sleep(0.2)
            try:
                if os.stat(path).st_ino != inode:
                    f.close()
                    f = open(path, "rb")  # noqa: SIM115
                    inode = os.fstat(f.fileno()).st_ino
            except FileNotFoundError:
                pass
    finally:
        f.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyagent engine",
        description="Run or manage the repo's engine: the process that owns "
        "the agents, so they keep running when the TUI quits.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.add_argument(
            "repo",
            nargs="?",
            default=None,
            help="Path to git repository (default: auto-detect from cwd)",
        )
        return sub

    start = command("start", "Start the repo's engine, detached, unless one runs")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="Run it in this terminal instead (Ctrl+C stops it and its agents)",
    )
    start.add_argument(
        "--timeout",
        type=float,
        default=launcher.START_TIMEOUT,
        help="Seconds to wait for it to serve (default: %(default)g)",
    )
    stop = command("stop", "Stop the repo's engine, and its agents and shells")
    stop.add_argument(
        "--if-idle",
        action="store_true",
        help="Only stop it when it runs no agent (an agent cannot outlive "
        "its engine)",
    )
    stop.add_argument(
        "--timeout",
        type=float,
        default=_STOP_TIMEOUT,
        help="Seconds to wait for it to exit (default: %(default)g)",
    )
    restart = command(
        "restart", "Stop the repo's engine and its agents, and start a new one"
    )
    restart.add_argument(
        "--restore",
        action="store_true",
        help="Then bring back the agents it stopped (older restorable ones stay offered)",
    )
    restart.add_argument("--json", action="store_true", help="Print JSON lines")
    restart.add_argument(
        "--timeout",
        type=float,
        default=_STOP_TIMEOUT,
        help="Seconds to wait for the old one to exit (default: %(default)g)",
    )
    status = command("status", "Whether an engine serves the repo (exit 3: none)")
    status.add_argument("--json", action="store_true", help="Print JSON")
    restore = command(
        "restore",
        "List, restore or dismiss the agents an earlier engine ran when it ended",
    )
    restore.add_argument(
        "--id",
        dest="ids",
        action="append",
        metavar="ID",
        help="Restore (or dismiss) this one; repeat for more",
    )
    restore.add_argument("--all", action="store_true", help="Every one of them")
    restore.add_argument(
        "--dismiss", action="store_true", help="Stop offering them instead"
    )
    restore.add_argument("--json", action="store_true", help="Print JSON")
    logs = command("logs", "Print the end of the engine's log")
    logs.add_argument("-n", "--lines", type=int, default=50, help="Lines to print")
    logs.add_argument("-f", "--follow", action="store_true", help="Keep printing")
    logs.add_argument("--path", action="store_true", help="Print the log's path")
    return parser


_COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "restart": cmd_restart,
    "status": cmd_status,
    "logs": cmd_logs,
    "restore": cmd_restore,
}


def build_status_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyagent status",
        description="Every work item of the repo (a branch with its worktree, "
        "agents and PR): who has the ball, a short fact and its age, the ones "
        "that need you first. Asks the repo's engine (exit 3: none runs).",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="Path to git repository (default: auto-detect from cwd)",
    )
    parser.add_argument("--json", action="store_true", help="Print status_list's JSON")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also merged, closed and stale branches",
    )
    parser.add_argument(
        "--why",
        metavar="KEY",
        help="Why this row (a key or a branch) is in its state: the rule, its "
        "file and line, the facts it read, the rules above that did not match",
    )
    return parser


def status_main(argv: list[str] | None = None) -> int:
    """``lazyagent status``: the engine's ``status_list`` as a table."""
    from lazyagent.engine.client import RequestError
    from lazyagent.engine.status import format_table
    from lazyagent.worktree_manager import WorktreeManagerError

    args = build_status_parser().parse_args(argv)

    def error(message: str) -> None:
        print(f"lazyagent status: {message}", file=sys.stderr, flush=True)

    try:
        root = launcher.resolve_repo_root(args.repo)
    except WorktreeManagerError as e:
        error(str(e))
        return 1
    socket = launcher.find(root)
    if socket is None:
        error(f"no engine running for {root} (start one: lazyagent engine start)")
        return NOT_RUNNING
    method, params = (("rules_explain", {"key": args.why}) if args.why
                      else ("status_list", {"include_done": args.all}))
    try:
        result = asyncio.run(_request(socket, method, **params))
    except RequestError as e:
        if e.code == "UNKNOWN_METHOD":
            error(
                f"this engine predates {method}; restart it "
                "(lazyagent engine restart --restore)"
            )
        else:
            error(e.message)
        return 1
    except (OSError, ConnectionError) as e:
        error(f"could not reach the engine: {e}")
        return 1
    if args.json:
        print(json.dumps(result))
    elif args.why:
        from lazyagent.engine.rules.export import format_explain

        print(format_explain(result), flush=True)
    else:
        print(format_table(result), flush=True)
    return 0


# ----------------------------------------------------------------------
# lazyagent rules
# ----------------------------------------------------------------------


def build_rules_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lazyagent rules",
        description="Your rules (rules.toml): the global file "
        "($XDG_CONFIG_HOME/lazyagent/rules.toml) and the repo's "
        "(.lazyagent/rules.toml, applied once you allow its hash).",
    )
    commands = parser.add_subparsers(dest="command", metavar="{check,show,allow,doc}")

    def command(name: str, help: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help, description=help)
        sub.add_argument("repo", nargs="?", default=None,
                         help="Path to git repository (default: auto-detect from cwd)")
        return sub

    check = command("check", "Validate the rule files (exit 1 on errors)")
    check.add_argument("--json", action="store_true", help="Print rules_check's JSON")
    show = command("show", "The effective rules, in order, and where each comes from")
    show.add_argument("--json", action="store_true", help="Print rules_catalog's JSON")
    allow = command("allow", "Allow the repo's .lazyagent/rules.toml at its current hash")
    allow.add_argument("hash", nargs="?", default=None,
                       help="The hash `rules check` shows (12+ characters); without it, "
                       "the file and its hash are printed")
    doc = command("doc", "The rules catalog as Markdown (the built-ins by default)")
    doc.add_argument("--effective", action="store_true",
                     help="With your rules (the files as they apply now)")
    return parser


def _rules_hub(root: str) -> Any:
    from lazyagent.engine.rules.hub import RulesHub

    return RulesHub(root)


def _problem_line(p: dict[str, Any]) -> str:
    where = p.get("file") or "rules"
    if p.get("line"):
        where += f":{p['line']}"
    return f"{where}: {p['message']} ({p['fallback']})"


def rules_main(argv: list[str] | None = None) -> int:
    """``lazyagent rules``: check, show, allow and doc. They read the files
    themselves (no engine needed); allow also tells a running engine."""
    from lazyagent.worktree_manager import WorktreeManagerError

    parser = build_rules_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2

    def error(message: str) -> None:
        print(f"lazyagent rules: {message}", file=sys.stderr, flush=True)

    try:
        root = launcher.resolve_repo_root(args.repo)
    except WorktreeManagerError as e:
        error(str(e))
        return 1
    hub = _rules_hub(root)
    if args.command == "check":
        result = hub.check_result()
        if args.json:
            print(json.dumps(result))
        else:
            for f in result["files"]:
                state = "absent" if not f["exists"] else f["applied"].replace("_", " ")
                extra = ""
                if f["scope"] == "repo" and f["exists"]:
                    extra = " · allowed" if f.get("allowed") else (
                        f" · not allowed (lazyagent rules allow {f['hash'][:12]})")
                print(f"{f['scope']:6} {f['path']}: {state}, {f['rules']} rules{extra}")
            for p in result["errors"]:
                print("error   " + _problem_line(p))
            for p in result["warnings"]:
                print("warning " + _problem_line(p))
            if not result["errors"]:
                print("no errors")
        return 1 if result["errors"] else 0
    if args.command == "show":
        from lazyagent.engine.rules.export import catalog_payload, format_rules

        payload = catalog_payload(hub.catalog, hub.check_result())
        print(json.dumps(payload) if args.json else format_rules(payload), flush=True)
        return 0
    if args.command == "doc":
        from lazyagent.engine.rules import builtin
        from lazyagent.engine.rules.export import markdown

        print(markdown(hub.catalog if args.effective else builtin()), end="", flush=True)
        return 0
    return _rules_allow(hub, root, args.hash, error)


def _rules_allow(hub: Any, root: str, hash_: str | None, error: Any) -> int:
    from lazyagent.engine.client import RequestError
    from lazyagent.engine.rules.hub import MIN_HASH, digest, repo_path

    path = repo_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        error(f"no {path}")
        return 1
    if hash_ is None:
        print(text, end="" if text.endswith("\n") else "\n")
        print(f"\n{path}: hash {digest(text)[:MIN_HASH]}. Read it; to apply it: "
              f"lazyagent rules allow {digest(text)[:MIN_HASH]}")
        return 0
    socket = launcher.find(root)
    try:
        if socket is not None:
            result = asyncio.run(_request(socket, "rules_allow", hash=hash_))
        else:
            result = hub.allow(hash_)
    except (ValueError, RequestError) as e:
        error(getattr(e, "message", None) or str(e))
        return 1
    except (OSError, ConnectionError) as e:
        error(f"could not reach the engine: {e}")
        return 1
    print(f"allowed {result['file']} at {result['hash'][:MIN_HASH]}"
          + ("" if socket is not None else " (no engine running: it applies on its start)"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    from lazyagent.worktree_manager import WorktreeManagerError

    try:
        root = launcher.resolve_repo_root(args.repo)
    except WorktreeManagerError as e:
        _error(str(e))
        return 1
    return _COMMANDS[args.command](args, root)
