"""``lazyagent update {check,apply}``: is there a newer lazyagent, and get it
(docs/design/desktop-update-restart-plan.md §2-§3).

Two modes:

- **published** (the default): the latest release on PyPI against this
  install's version; applied by the installer that made this install
  (``uv tool upgrade`` or ``pipx upgrade``), else only the command is shown.
- **development**: lazyagent runs from a git checkout (an editable install,
  or a desktop binary under the checkout's ``desktop/target/``). The
  checkout's branch is compared with its upstream, and an update is a
  fast-forward (``git merge --ff-only``), then ``uv sync`` when the lock
  changed and the app's build when ``desktop/`` changed. Uncommitted changes
  stay as they are; when git would overwrite some (or untracked files), the
  update is refused with their list and nothing changes. Never a stash,
  reset, checkout, clean or rebase.

``check --json`` prints one object; ``apply --json`` prints one JSON object
per line (``{"step", …}`` progress, then ``{"result": …}``), which the
desktop app reads to show progress. Standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

from lazyagent import __version__

# src/lazyagent/update.py -> the source tree's root (as desktop.py's).
SOURCE_ROOT = Path(__file__).resolve().parents[2]
PYPI_URL = "https://pypi.org/pypi/lazyagent/json"
# Where to read the release list instead (tests: a file:// URL).
INDEX_ENV = "LAZYAGENT_UPDATE_INDEX"
COMMITS_SHOWN = 30
_GIT_TIMEOUT = 60.0
# Files whose change means the engine runs other code, the env must sync, or
# the app must be rebuilt.
_PYTHON_PATHS = ("src/", "pyproject.toml")
_LOCK_PATHS = ("uv.lock", "pyproject.toml")
_DESKTOP_PATHS = ("desktop/",)
_IN_PROGRESS = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")

Emit = Callable[[dict[str, Any]], None]


def _git(
    root: Path, *args: str, timeout: float = _GIT_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def _out(root: Path, *args: str) -> str | None:
    """The command's stdout, stripped; None when it fails."""
    try:
        result = _git(root, *args)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def is_checkout(root: Path) -> bool:
    """A lazyagent source tree under git."""
    try:
        pyproject = (root / "pyproject.toml").read_text()
    except OSError:
        return False
    return (root / ".git").exists() and re.search(
        r'^name\s*=\s*"lazyagent"', pyproject, re.M
    ) is not None


@functools.cache
def source_commit() -> str | None:
    """The commit of the lazyagent source this process runs, when it runs
    from a checkout (an engine notes it at start: ``hello.source_commit``)."""
    if not is_checkout(SOURCE_ROOT):
        return None
    return _out(SOURCE_ROOT, "rev-parse", "HEAD")


def _editable() -> bool:
    try:
        text = metadata.distribution("lazyagent").read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return False
    try:
        return bool(json.loads(text or "{}").get("dir_info", {}).get("editable"))
    except ValueError:
        return False


def _checkout_of(binary: str | None) -> Path | None:
    """The checkout whose ``desktop/target/`` holds ``binary``."""
    if not binary:
        return None
    path = Path(binary).resolve()
    for parent in path.parents:
        if parent.name == "target" and parent.parent.name == "desktop":
            root = parent.parent.parent
            return root if is_checkout(root) else None
    return None


def detect(
    source: Path | None = None, app_binary: str | None = None
) -> tuple[str, Path | None]:
    """``("development", checkout)`` or ``("published", None)``."""
    if source is not None:
        return "development", source
    if _editable() and is_checkout(SOURCE_ROOT):
        return "development", SOURCE_ROOT
    built = _checkout_of(app_binary)
    if built is not None:
        return "development", built
    return "published", None


# ----------------------------------------------------------------------
# development mode
# ----------------------------------------------------------------------


def _split_z(text: str) -> list[str]:
    return [p for p in text.split("\0") if p]


def _dirty_paths(root: Path) -> list[str]:
    """Paths with uncommitted changes, untracked files included."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    paths: list[str] = []
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":  # a rename's or copy's source follows
            if i < len(fields) and fields[i]:
                paths.append(fields[i])
            i += 1
    return paths


def _touches(paths: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(p == x or p.startswith(x) for p in paths for x in prefixes)


def _in_progress(root: Path) -> str | None:
    for name in _IN_PROGRESS:
        rel = _out(root, "rev-parse", "--git-path", name)
        if rel is None:
            continue
        path = Path(rel) if os.path.isabs(rel) else root / rel
        if path.exists():
            return name
    return None


def _app_behind(root: Path, app_commit: str | None) -> bool | None:
    """Whether the app binary was built before the checkout's last change
    under ``desktop/`` (None: unknown)."""
    if not app_commit:
        return None
    built = app_commit.split("+", 1)[0]
    last = _out(root, "log", "-1", "--format=%H", "HEAD", "--", "desktop")
    if not last or _out(root, "cat-file", "-e", f"{built}^{{commit}}") is None:
        return None
    try:
        known = _git(root, "merge-base", "--is-ancestor", last, built).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    return known != 0


def check_development(
    root: Path,
    *,
    fetch: bool = True,
    engine_commit: str | None = None,
    app_commit: str | None = None,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "mode": "development",
        "version": __version__,
        "root": str(root),
        "branch": None,
        "upstream": None,
        "head": None,
        "ahead": 0,
        "behind": 0,
        "commits": [],
        "changes": {"python": False, "lock": False, "desktop": False},
        "collisions": [],
        "update_available": False,
        "can_apply": False,
        "reason": None,
        "fetch_error": None,
        "engine_commit": engine_commit,
        "engine_behind": None,
        "app_commit": app_commit,
        "app_behind": None,
        "latest": None,
        "installer": None,
        "command": None,
    }
    head = _out(root, "rev-parse", "HEAD")
    if head is None:
        info["reason"] = f"{root} is not a git checkout"
        return info
    info["head"] = head
    info["engine_behind"] = bool(engine_commit) and engine_commit != head
    info["app_behind"] = _app_behind(root, app_commit)
    branch = _out(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    info["branch"] = branch
    if branch is None:
        info["reason"] = "HEAD is detached: check out a branch to update it"
        return info
    upstream = _out(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    info["upstream"] = upstream
    if upstream is None:
        info["reason"] = f"branch {branch} has no upstream to update from"
        return info
    if fetch:
        remote = _out(root, "config", f"branch.{branch}.remote")
        merge = _out(root, "config", f"branch.{branch}.merge")
        if remote and remote != "." and merge:
            ref = merge.removeprefix("refs/heads/")
            try:
                fetched = _git(root, "fetch", "--quiet", "--no-tags", remote, ref)
                if fetched.returncode != 0:
                    info["fetch_error"] = fetched.stderr.strip() or "git fetch failed"
            except (OSError, subprocess.TimeoutExpired) as e:
                info["fetch_error"] = f"git fetch: {e}"
    counts = _out(root, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    ahead, behind = (int(n) for n in (counts or "0 0").split())
    info["ahead"], info["behind"] = ahead, behind
    info["update_available"] = behind > 0
    log = _out(root, "log", f"-n{COMMITS_SHOWN}", "--format=%h%x09%s", "HEAD..@{u}")
    info["commits"] = [
        dict(zip(("sha", "subject"), line.split("\t", 1), strict=False))
        for line in (log or "").splitlines()
    ]
    changed = _split_z(
        _git(root, "diff", "--name-only", "-z", "--no-renames", "HEAD...@{u}").stdout
    )
    info["changes"] = {
        "python": _touches(changed, _PYTHON_PATHS),
        "lock": _touches(changed, _LOCK_PATHS),
        "desktop": _touches(changed, _DESKTOP_PATHS),
    }
    info["collisions"] = sorted(set(_dirty_paths(root)) & set(changed))
    busy = _in_progress(root)
    if busy is not None:
        info["reason"] = f"a git operation is in progress in {root} ({busy})"
    elif ahead:
        info["reason"] = (
            f"{branch} has {ahead} commit{'s' if ahead != 1 else ''} not on "
            f"{upstream}: update it by hand"
        )
    elif not behind:
        info["reason"] = None
    elif info["collisions"]:
        info["reason"] = (
            "the update would overwrite uncommitted changes or untracked "
            "files: " + ", ".join(info["collisions"])
        )
    info["can_apply"] = behind > 0 and info["reason"] is None
    return info


_OVERWRITTEN = re.compile(r"would be overwritten by merge")


def _overwritten(stderr: str) -> list[str]:
    """The files git lists when it refuses a merge over local changes."""
    files: list[str] = []
    listing = False
    for line in stderr.splitlines():
        if _OVERWRITTEN.search(line):
            listing = True
        elif listing and line.startswith("\t"):
            files.append(line.strip())
        else:
            listing = False
    return files


def _tool(name: str) -> str | None:
    """``name`` on PATH, else in the usual per-user install dirs."""
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    for d in (home / ".local" / "bin", home / ".cargo" / "bin"):
        if (d / name).is_file() and os.access(d / name, os.X_OK):
            return str(d / name)
    return None


def _stream(step: str, command: list[str], cwd: Path, emit: Emit) -> int:
    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    emit({"step": step, "state": "running", "detail": " ".join(command)})
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
    except OSError as e:
        emit({"step": step, "state": "failed", "detail": str(e)})
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        emit({"step": step, "output": line.rstrip("\n")})
    code = proc.wait()
    emit({"step": step, "state": "done" if code == 0 else "failed", "code": code})
    return code


def _build_command(root: Path, app_binary: str | None) -> list[str]:
    """Rebuild the app the way the running one was installed."""
    if _checkout_of(app_binary) == root:
        return ["build", "--release", "--locked", "--manifest-path", "desktop/Cargo.toml"]
    return ["install", "--locked", "--path", "desktop/crates/app"]


def apply_development(
    root: Path,
    emit: Emit,
    *,
    engine_commit: str | None = None,
    app_commit: str | None = None,
    app_binary: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    emit({"step": "check", "state": "running"})
    info = check_development(
        root, fetch=fetch, engine_commit=engine_commit, app_commit=app_commit
    )
    result: dict[str, Any] = {"ok": False, "check": info, "steps": [], "files": []}
    if info["fetch_error"]:
        result["reason"] = f"could not fetch: {info['fetch_error']}"
        return result
    if not info["update_available"]:
        result.update(ok=True, reason="already up to date")
        return result
    if not info["can_apply"]:
        result.update(reason=info["reason"], files=info["collisions"])
        return result
    emit({"step": "merge", "state": "running", "detail": f"git merge --ff-only {info['upstream']}"})
    merged = _git(root, "merge", "--ff-only", "--quiet", "@{u}")
    if merged.returncode != 0:
        files = _overwritten(merged.stderr)
        emit({"step": "merge", "state": "failed", "detail": merged.stderr.strip()})
        result["files"] = files
        result["reason"] = (
            "git refused the update; it would overwrite: " + ", ".join(files)
            if files
            else merged.stderr.strip() or "git merge --ff-only failed"
        )
        return result
    result["steps"].append("merge")
    result["head"] = _out(root, "rev-parse", "HEAD")
    emit({"step": "merge", "state": "done", "detail": result["head"]})
    failed: list[str] = []
    if info["changes"]["lock"]:
        uv = _tool("uv")
        if uv is None:
            failed.append("uv sync (uv not found: run `uv sync` in the checkout)")
        elif _stream("uv-sync", [uv, "sync"], root, emit) != 0:
            failed.append("uv sync")
        else:
            result["steps"].append("uv-sync")
    if info["changes"]["desktop"] or info["app_behind"]:
        cargo = _tool("cargo")
        args = _build_command(root, app_binary)
        if cargo is None:
            failed.append(f"app build (cargo not found: run `cargo {' '.join(args)}`)")
        elif _stream("build", [cargo, *args], root, emit) != 0:
            failed.append("app build")
        else:
            result["steps"].append("build")
    result["ok"] = not failed
    if failed:
        result["reason"] = "updated, but these steps failed: " + "; ".join(failed)
    result["restart"] = {
        "app": True,
        "engine": info["changes"]["python"] or bool(info["engine_behind"]),
    }
    return result


# ----------------------------------------------------------------------
# published mode
# ----------------------------------------------------------------------


def _version_key(version: str) -> tuple[int, ...] | None:
    """A final release's key (``0.8.0`` → (0, 8, 0)); None for anything
    else (pre-releases, dev builds, local versions)."""
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(n) for n in version.split("."))


def latest_release(url: str | None = None) -> str:
    """The newest final, non-yanked release on the index."""
    url = url or os.environ.get(INDEX_ENV) or PYPI_URL
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 (https or a test's file://)
        data = json.load(response)
    best: tuple[tuple[int, ...], str] | None = None
    for version, files in (data.get("releases") or {}).items():
        key = _version_key(version)
        if key is None or not files or all(f.get("yanked") for f in files):
            continue
        if best is None or key > best[0]:
            best = (key, version)
    if best is None:
        raise ValueError("the index lists no release")
    return best[1]


def installer(prefix: str = sys.prefix, environ: dict[str, str] | None = None) -> str:
    """``uv-tool``, ``pipx`` or ``other``: what made this install."""
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME") or Path.home())
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    here = Path(prefix).resolve()

    def under(*dirs: Path) -> bool:
        return any(here.is_relative_to(d.resolve()) for d in dirs)

    uv_tools = [Path(env["UV_TOOL_DIR"])] if env.get("UV_TOOL_DIR") else []
    if under(*uv_tools, data / "uv" / "tools"):
        return "uv-tool"
    pipx = [Path(env["PIPX_HOME"]) / "venvs"] if env.get("PIPX_HOME") else []
    if under(*pipx, home / ".local" / "pipx" / "venvs", data / "pipx" / "venvs"):
        return "pipx"
    return "other"


def upgrade_command(kind: str) -> list[str]:
    if kind == "uv-tool":
        return ["uv", "tool", "upgrade", "lazyagent"]
    if kind == "pipx":
        return ["pipx", "upgrade", "lazyagent"]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "lazyagent"]


def check_published(*, engine_commit: str | None = None) -> dict[str, Any]:
    kind = installer()
    info: dict[str, Any] = {
        "mode": "published",
        "version": __version__,
        "root": None,
        "latest": None,
        "installer": kind,
        "command": upgrade_command(kind),
        "update_available": False,
        "can_apply": False,
        "reason": None,
        "fetch_error": None,
        "commits": [],
        "changes": {"python": False, "lock": False, "desktop": False},
        "collisions": [],
        "engine_commit": engine_commit,
        "engine_behind": None,
        "app_behind": None,
    }
    try:
        latest = latest_release()
    except (OSError, ValueError) as e:
        info["fetch_error"] = f"could not read the release list: {e}"
        return info
    info["latest"] = latest
    current = _version_key(__version__)
    newer = current is None or _version_key(latest) > current  # type: ignore[operator]
    info["update_available"] = bool(newer) and latest != __version__
    if info["update_available"]:
        info["changes"] = {"python": True, "lock": False, "desktop": True}
        if kind == "other":
            info["reason"] = (
                "this install was not made by `uv tool` or pipx; upgrade it "
                f"with: {' '.join(info['command'])}"
            )
    info["can_apply"] = info["update_available"] and kind != "other"
    return info


def apply_published(emit: Emit, *, engine_commit: str | None = None) -> dict[str, Any]:
    emit({"step": "check", "state": "running"})
    info = check_published(engine_commit=engine_commit)
    result: dict[str, Any] = {"ok": False, "check": info, "steps": [], "files": []}
    if info["fetch_error"]:
        result["reason"] = info["fetch_error"]
        return result
    if not info["update_available"]:
        result.update(ok=True, reason="already up to date")
        return result
    if not info["can_apply"]:
        result["reason"] = info["reason"]
        return result
    command = info["command"]
    tool = _tool(command[0])
    if tool is None:
        result["reason"] = f"{command[0]} not found: run `{' '.join(command)}`"
        return result
    if _stream("upgrade", [tool, *command[1:]], Path.home(), emit) != 0:
        result["reason"] = f"`{' '.join(command)}` failed"
        return result
    result.update(ok=True, steps=["upgrade"], restart={"app": True, "engine": True})
    return result


# ----------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------


def check(
    *,
    source: Path | None = None,
    app_binary: str | None = None,
    app_commit: str | None = None,
    engine_commit: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    mode, root = detect(source, app_binary)
    if mode == "development" and root is not None:
        return check_development(
            root, fetch=fetch, engine_commit=engine_commit, app_commit=app_commit
        )
    return check_published(engine_commit=engine_commit)


@contextlib.contextmanager
def _update_lock() -> Iterator[bool]:
    from lazyagent.engine.runtime import state_dir

    path = state_dir() / "update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def apply(
    emit: Emit,
    *,
    source: Path | None = None,
    app_binary: str | None = None,
    app_commit: str | None = None,
    engine_commit: str | None = None,
    fetch: bool = True,
) -> dict[str, Any]:
    with _update_lock() as mine:
        if not mine:
            return {"ok": False, "reason": "another update is running", "steps": [], "files": []}
        mode, root = detect(source, app_binary)
        if mode == "development" and root is not None:
            return apply_development(
                root,
                emit,
                engine_commit=engine_commit,
                app_commit=app_commit,
                app_binary=app_binary,
                fetch=fetch,
            )
        return apply_published(emit, engine_commit=engine_commit)


def _describe(info: dict[str, Any]) -> list[str]:
    lines = []
    if info["mode"] == "development":
        lines.append(f"development checkout {info['root']} ({info['branch']} → {info['upstream']})")
        if info["update_available"]:
            lines.append(f"{info['behind']} new commit(s):")
            lines += [f"  {c['sha']} {c.get('subject', '')}" for c in info["commits"]]
        elif info["upstream"]:
            lines.append("up to date")
    else:
        lines.append(f"published release {info['version']} (installed by {info['installer']})")
        if info["latest"]:
            lines.append(
                f"lazyagent {info['latest']} is available"
                if info["update_available"]
                else "up to date"
            )
    for key in ("fetch_error", "reason"):
        if info.get(key):
            lines.append(info[key])
    if info.get("engine_behind"):
        lines.append("the engine runs older code than the checkout: restart it")
    if info.get("app_behind"):
        lines.append("the desktop app was built from older code: rebuild it")
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="lazyagent update",
        description="Check for a newer lazyagent, or get it (a published "
        "release, or the git checkout lazyagent runs from).",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name, help in (("check", "Is there an update?"), ("apply", "Get it")):
        sub = commands.add_parser(name, help=help, description=help)
        sub.add_argument("--json", action="store_true", help="Print JSON")
        sub.add_argument("--no-fetch", action="store_true", help="Do not fetch (development mode)")
        sub.add_argument("--source", type=Path, help="Use this lazyagent checkout")
        sub.add_argument("--app-binary", help="The desktop binary that runs")
        sub.add_argument("--app-commit", help="The commit the desktop binary was built from")
        sub.add_argument("--engine-commit", help="The commit the engine runs (hello.source_commit)")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    options = {
        "source": args.source.resolve() if args.source else None,
        "app_binary": args.app_binary,
        "app_commit": args.app_commit,
        "engine_commit": args.engine_commit,
        "fetch": not args.no_fetch,
    }
    if args.command == "check":
        info = check(**options)
        if args.json:
            print(json.dumps(info))
        else:
            for line in _describe(info):
                print(f"lazyagent update: {line}")
        return 0 if info["fetch_error"] is None else 1

    def emit(event: dict[str, Any]) -> None:
        if args.json:
            print(json.dumps(event), flush=True)
        elif "output" in event:
            print(event["output"], flush=True)
        elif event.get("state") == "running":
            print(f"lazyagent update: {event['step']}…", flush=True)

    result = apply(emit, **options)
    if args.json:
        print(json.dumps({"result": result}), flush=True)
    else:
        print(f"lazyagent update: {'done' if result['ok'] else 'not updated'}"
              + (f": {result['reason']}" if result.get("reason") else ""))
    return 0 if result["ok"] else 1
