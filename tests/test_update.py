"""``lazyagent update``: published releases and a development checkout.

Every git test works on a bare remote and a clone of it under ``tmp_path``
(``--source``), never on the checkout the suite runs from; ``uv`` and
``cargo`` are shims on PATH that record their arguments.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lazyagent import __version__, update


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A clone of a bare remote on ``develop``; ``upstream`` pushes to it."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(remote))
    upstream = tmp_path / "upstream"
    _git(tmp_path, "clone", "-q", str(remote), str(upstream))
    _commit(
        upstream,
        {
            "pyproject.toml": 'name = "lazyagent"\n',
            "uv.lock": "v1\n",
            "src/lazyagent/a.py": "a = 1\n",
            "desktop/app.rs": "fn main() {}\n",
            "README.md": "hi\n",
        },
        "init",
    )
    _git(upstream, "push", "-q", "origin", "HEAD:develop")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", "-b", "develop", str(remote), str(local))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    for tool in ("uv", "cargo"):
        shim = bin_dir / tool
        shim.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"open({str(calls)!r}, 'a').write(json.dumps([{tool!r}, os.getcwd(), sys.argv[1:]]) + '\\n')\n"
            "print('built')\n"
            "sys.exit(int(os.environ.get('FAKE_TOOL_EXIT', '0')))\n"
        )
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    class Checkout:
        root = local
        up = upstream

        def push(self, files: dict[str, str], message: str = "change") -> None:
            _commit(upstream, files, message)
            _git(upstream, "push", "-q", "origin", "HEAD:develop")

        def calls(self) -> list:
            if not calls.exists():
                return []
            return [json.loads(line) for line in calls.read_text().splitlines()]

    return Checkout()


def _apply(root: Path, **options):
    events: list[dict] = []
    result = update.apply(events.append, source=root, **options)
    return result, events


def test_up_to_date_then_behind(checkout):
    info = update.check(source=checkout.root)
    assert info["mode"] == "development" and info["branch"] == "develop"
    assert info["upstream"] == "origin/develop"
    assert not info["update_available"] and info["reason"] is None

    checkout.push({"README.md": "one\n"}, "first")
    checkout.push({"src/lazyagent/a.py": "a = 2\n"}, "second")
    assert not update.check(source=checkout.root, fetch=False)["update_available"]
    info = update.check(source=checkout.root)
    assert info["behind"] == 2 and info["ahead"] == 0 and info["can_apply"]
    assert [c["subject"] for c in info["commits"]] == ["second", "first"]
    assert info["changes"] == {"python": True, "lock": False, "desktop": False}


def test_apply_fast_forwards_and_keeps_changes_that_do_not_collide(checkout):
    (checkout.root / "README.md").write_text("mine\n")  # tracked, modified
    (checkout.root / "notes.txt").write_text("untracked\n")
    checkout.push({"src/lazyagent/a.py": "a = 3\n"})
    result, events = _apply(checkout.root)
    assert result["ok"], result
    assert result["steps"] == ["merge"]
    assert result["restart"] == {"app": True, "engine": True}
    assert (checkout.root / "src/lazyagent/a.py").read_text() == "a = 3\n"
    assert (checkout.root / "README.md").read_text() == "mine\n"
    assert (checkout.root / "notes.txt").read_text() == "untracked\n"
    assert _git(checkout.root, "rev-parse", "HEAD") == _git(checkout.up, "rev-parse", "HEAD")
    assert {"step": "merge", "state": "running", "detail": "git merge --ff-only origin/develop"} in events
    assert checkout.calls() == [], "nothing to sync or build"


def test_a_change_the_update_would_overwrite_refuses_it_and_changes_nothing(checkout):
    (checkout.root / "README.md").write_text("mine\n")
    checkout.push({"README.md": "theirs\n"})
    head = _git(checkout.root, "rev-parse", "HEAD")
    info = update.check(source=checkout.root)
    assert info["collisions"] == ["README.md"] and not info["can_apply"]
    assert "README.md" in info["reason"]
    result, _ = _apply(checkout.root)
    assert not result["ok"] and result["files"] == ["README.md"]
    assert (checkout.root / "README.md").read_text() == "mine\n"
    assert _git(checkout.root, "rev-parse", "HEAD") == head


def test_an_untracked_file_the_update_adds_refuses_it(checkout):
    (checkout.root / "new.txt").write_text("mine\n")
    checkout.push({"new.txt": "theirs\n"})
    result, _ = _apply(checkout.root)
    assert not result["ok"] and result["files"] == ["new.txt"]
    assert (checkout.root / "new.txt").read_text() == "mine\n"


def test_git_itself_refuses_when_the_check_could_not_see_it(checkout, monkeypatch):
    """The check is a prediction; git's own refusal is reported the same way."""
    (checkout.root / "README.md").write_text("mine\n")
    checkout.push({"README.md": "theirs\n"})
    monkeypatch.setattr(update, "_dirty_paths", lambda root: [])
    result, events = _apply(checkout.root)
    assert not result["ok"] and result["files"] == ["README.md"]
    assert "would overwrite: README.md" in result["reason"]
    assert any(e.get("state") == "failed" for e in events if e["step"] == "merge")
    assert (checkout.root / "README.md").read_text() == "mine\n"


def test_local_commits_or_a_detached_head_are_refused(checkout):
    checkout.push({"README.md": "theirs\n"})
    _commit(checkout.root, {"mine.txt": "x\n"}, "local")
    info = update.check(source=checkout.root)
    assert info["ahead"] == 1 and info["behind"] == 1 and not info["can_apply"]
    assert "update it by hand" in info["reason"]
    head = _git(checkout.root, "rev-parse", "HEAD")
    result, _ = _apply(checkout.root)
    assert not result["ok"] and _git(checkout.root, "rev-parse", "HEAD") == head

    _git(checkout.root, "checkout", "-q", "--detach")
    info = update.check(source=checkout.root)
    assert info["branch"] is None and "detached" in info["reason"]


def test_a_merge_in_progress_is_refused(checkout):
    checkout.push({"README.md": "theirs\n"})
    _git(checkout.root, "fetch", "-q")
    (Path(_git(checkout.root, "rev-parse", "--absolute-git-dir")) / "MERGE_HEAD").write_text(
        _git(checkout.root, "rev-parse", "origin/develop") + "\n"
    )
    info = update.check(source=checkout.root)
    assert not info["can_apply"] and "MERGE_HEAD" in info["reason"]


def test_lock_and_desktop_changes_sync_and_build(checkout):
    checkout.push({"uv.lock": "v2\n", "desktop/app.rs": "fn main() { }\n"})
    result, events = _apply(checkout.root)
    assert result["ok"], result
    assert result["steps"] == ["merge", "uv-sync", "build"]
    assert result["restart"] == {"app": True, "engine": False}, "no Python changed"
    calls = checkout.calls()
    assert calls == [
        ["uv", str(checkout.root), ["sync"]],
        ["cargo", str(checkout.root), ["install", "--locked", "--path", "desktop/crates/app"]],
    ]
    assert {"step": "build", "output": "built"} in events

    # A binary built in the checkout's desktop/target is rebuilt in place.
    checkout.push({"desktop/app.rs": "fn main() {  }\n"})
    binary = checkout.root / "desktop/target/release/lazyagent-desktop"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    result, _ = _apply(checkout.root, app_binary=str(binary))
    assert result["ok"] and checkout.calls()[-1][2][:2] == ["build", "--release"]


def test_engine_and_app_behind(checkout):
    first = _git(checkout.root, "rev-parse", "HEAD")
    checkout.push({"desktop/app.rs": "fn main() { 1; }\n"})
    _git(checkout.root, "pull", "-q", "--ff-only")
    head = _git(checkout.root, "rev-parse", "HEAD")
    info = update.check(source=checkout.root, engine_commit=first, app_commit=first)
    assert info["engine_behind"] and info["app_behind"]
    info = update.check(source=checkout.root, engine_commit=head, app_commit=f"{head}+dirty")
    assert not info["engine_behind"] and info["app_behind"] is False
    assert update.check(source=checkout.root, app_commit="f" * 40)["app_behind"] is None


def _built_app(root: Path, age: float = 0) -> Path:
    """A release build in the checkout, ``age`` seconds older than now."""
    binary = root / "desktop/target/release/lazyagent-desktop"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("")
    when = binary.stat().st_mtime - age
    os.utime(binary, (when, when))
    return binary


def test_pulled_by_hand_the_update_rebuilds_the_app(checkout):
    """Nothing left to pull, but the app predates the checkout: the update
    is the build, and only then is the app's restart offered."""
    first = _git(checkout.root, "rev-parse", "HEAD")
    checkout.push({"desktop/app.rs": "fn main() { 2; }\n"})
    _git(checkout.root, "pull", "-q", "--ff-only")
    binary = _built_app(checkout.root)
    info = update.check(source=checkout.root, app_commit=first, app_binary=str(binary))
    assert not info["update_available"] and info["app_behind"]
    result, events = _apply(checkout.root, app_commit=first, app_binary=str(binary))
    assert result["ok"], result
    assert result["steps"] == ["build"]
    assert result["restart"] == {"app": True, "engine": False}
    assert checkout.calls() == [["cargo", str(checkout.root), list(update.desktop.RELEASE_BUILD)]]
    assert {"step": "build", "state": "done", "code": 0} in events
    # Up to date and built from HEAD: nothing to do.
    head = _git(checkout.root, "rev-parse", "HEAD")
    result, _ = _apply(checkout.root, app_commit=head, app_binary=str(binary))
    assert result["ok"] and result["reason"] == "already up to date" and not result["steps"]
    assert len(checkout.calls()) == 1


def test_a_failed_build_offers_no_restart_of_the_app(checkout, monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_EXIT", "101")
    checkout.push({"desktop/app.rs": "fn main() { 3; }\n", "src/lazyagent/a.py": "a = 3\n"})
    binary = _built_app(checkout.root)
    first = _git(checkout.root, "rev-parse", "HEAD")
    result, events = _apply(checkout.root, app_commit=first, app_binary=str(binary))
    assert not result["ok"]
    assert result["steps"] == ["merge"], "the pull stays; the build did not happen"
    assert result["reason"] == "updated, but these steps failed: app build"
    assert result["restart"] == {"app": False, "engine": True}
    assert {"step": "build", "state": "failed", "code": 101} in events
    # Retried with nothing left to pull: still no restart of the app.
    result, _ = _apply(checkout.root, app_commit=first, app_binary=str(binary))
    assert result["steps"] == [] and result["reason"] == "failed: app build"
    assert result["restart"]["app"] is False


def test_without_cargo_the_rebuild_names_the_command(checkout, monkeypatch):
    first = _git(checkout.root, "rev-parse", "HEAD")
    checkout.push({"desktop/app.rs": "fn main() { 4; }\n"})
    _git(checkout.root, "pull", "-q", "--ff-only")
    binary = _built_app(checkout.root)
    monkeypatch.setattr(update, "_tool", lambda name: None)
    result, _ = _apply(checkout.root, app_commit=first, app_binary=str(binary))
    assert not result["ok"] and result["restart"]["app"] is False
    assert "cargo build --release" in result["reason"]


def test_app_behind_its_sources_by_time(checkout):
    """A build in the checkout without a known commit (or one that knows no
    better) is compared with its sources' modification times."""
    src = checkout.root / "desktop/crates/app/src/main.rs"
    src.parent.mkdir(parents=True)
    src.write_text("fn main() {}\n")
    binary = _built_app(checkout.root, age=60)
    head = _git(checkout.root, "rev-parse", "HEAD")
    for commit in (None, head):
        info = update.check(source=checkout.root, app_commit=commit, app_binary=str(binary))
        assert info["app_behind"] is True
    result, _ = _apply(checkout.root, app_commit=head, app_binary=str(binary))
    assert result["steps"] == ["build"] and result["restart"]["app"]
    # The fake cargo built nothing (as cargo does when nothing changed): the
    # binary is marked built, so the next check does not ask again.
    info = update.check(source=checkout.root, app_commit=head, app_binary=str(binary))
    assert info["app_behind"] is False


def test_mode_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_editable", lambda: False)
    assert update.detect() == ("published", None)
    tree = tmp_path / "src"
    (tree / ".git").mkdir(parents=True)
    (tree / "pyproject.toml").write_text('[project]\nname = "lazyagent"\n')
    binary = tree / "desktop/target/release/lazyagent-desktop"
    assert update.detect(app_binary=str(binary)) == ("development", tree)
    assert update.detect(app_binary="/usr/bin/lazyagent-desktop") == ("published", None)
    monkeypatch.setattr(update, "_editable", lambda: True)
    monkeypatch.setattr(update, "SOURCE_ROOT", tree)
    assert update.detect() == ("development", tree)


def _index(tmp_path: Path, releases: dict) -> str:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"releases": releases}))
    return path.as_uri()


def test_published_latest_release(tmp_path, monkeypatch):
    file = [{"yanked": False}]
    monkeypatch.setenv(
        update.INDEX_ENV,
        _index(tmp_path, {"0.8.0": file, "99.0.0": file, "100.0.0rc1": file,
                          "101.0.0": [{"yanked": True}], "102.0.0": []}),
    )
    monkeypatch.setattr(update, "installer", lambda: "uv-tool")
    info = update.check_published()
    assert info["latest"] == "99.0.0" and info["update_available"] and info["can_apply"]
    assert info["command"] == ["uv", "tool", "upgrade", "lazyagent"]

    monkeypatch.setenv(update.INDEX_ENV, _index(tmp_path, {__version__: file}))
    assert not update.check_published()["update_available"]

    monkeypatch.setenv(update.INDEX_ENV, (tmp_path / "missing.json").as_uri())
    info = update.check_published()
    assert info["fetch_error"] and not info["update_available"]


def test_published_other_installers_only_show_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv(update.INDEX_ENV, _index(tmp_path, {"99.0.0": [{}]}))
    monkeypatch.setattr(update, "installer", lambda: "other")
    info = update.check_published()
    assert info["update_available"] and not info["can_apply"]
    assert "pip" in info["reason"]
    events: list = []
    assert not update.apply_published(events.append)["ok"]


def test_installer(tmp_path):
    env = {"HOME": str(tmp_path)}
    tools = tmp_path / ".local/share/uv/tools/lazyagent"
    pipx = tmp_path / ".local/pipx/venvs/lazyagent"
    for d in (tools, pipx):
        d.mkdir(parents=True)
    assert update.installer(str(tools), env) == "uv-tool"
    assert update.installer(str(pipx), env) == "pipx"
    assert update.installer(str(tmp_path / "venv"), env) == "other"
    assert update.installer(str(tmp_path / "t"), {**env, "UV_TOOL_DIR": str(tmp_path)}) == "uv-tool"


def test_one_update_at_a_time(checkout):
    with update._update_lock() as mine:
        assert mine
        result, _ = _apply(checkout.root)
        assert not result["ok"] and result["reason"] == "another update is running"


def test_cli_json(checkout):
    checkout.push({"README.md": "two\n"})
    run = subprocess.run(
        [sys.executable, "-m", "lazyagent", "update", "check", "--json", "--source", str(checkout.root)],
        capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["behind"] == 1
    run = subprocess.run(
        [sys.executable, "-m", "lazyagent", "update", "apply", "--json", "--source", str(checkout.root)],
        capture_output=True, text=True, check=False,
    )
    lines = [json.loads(line) for line in run.stdout.splitlines()]
    assert run.returncode == 0 and lines[-1]["result"]["ok"]
    assert lines[0] == {"step": "check", "state": "running"}
