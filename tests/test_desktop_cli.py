"""``lazyagent desktop``: finding the Rust binary and handing over to it."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from lazyagent import desktop


def _exe(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_binary_lookup_order(tmp_path):
    src = tmp_path / "src"
    built = src / "desktop" / "target" / "release" / "lazyagent-desktop"
    built.parent.mkdir(parents=True)
    on_path_dir = tmp_path / "bin"
    on_path_dir.mkdir()
    explicit = _exe(tmp_path / "explicit")

    env = {"PATH": str(on_path_dir)}
    assert desktop.find_binary(env, src) is None
    _exe(built)
    assert desktop.find_binary(env, src) == built
    on_path = _exe(on_path_dir / "lazyagent-desktop")
    assert desktop.find_binary(env, src) == on_path
    with_explicit = {**env, desktop.BIN_ENV: str(explicit)}
    assert desktop.find_binary(with_explicit, src) == explicit
    # An explicit path that is not runnable is an error, not a fallback.
    missing = {**env, desktop.BIN_ENV: str(tmp_path / "nope")}
    assert desktop.find_binary(missing, src) is None


def test_hands_over_with_this_python(tmp_path, monkeypatch):
    binary = _exe(tmp_path / "lazyagent-desktop")
    monkeypatch.setenv(desktop.BIN_ENV, str(binary))
    calls = []
    monkeypatch.setattr(
        os, "execve", lambda path, args, env: calls.append((path, args, env))
    )
    desktop.main(["/some/repo", "--theme", "light"])
    (path, args, env), = calls
    assert path == binary
    assert args == [str(binary), "/some/repo", "--theme", "light"]
    assert env["LAZYAGENT_PYTHON"] == sys.executable


def test_missing_binary_explains_how_to_build(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(desktop.BIN_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(desktop, "SOURCE_ROOT", tmp_path)
    monkeypatch.setattr(desktop, "find_binary", lambda: None)
    assert desktop.main([]) == 2
    err = capsys.readouterr().err
    assert "cargo install" in err and "desktop/crates/app" in err


@pytest.mark.parametrize("argv", [["desktop", "--help"]])
def test_cli_dispatches_without_textual(argv):
    """The subcommand runs before the TUI imports (no Textual load)."""
    code = (
        "import sys; from lazyagent.cli import main\n"
        "try:\n    main(%r)\nexcept SystemExit as e:\n    print('exit', e.code)\n"
        "print('textual' in sys.modules)\n" % argv
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert "usage: lazyagent desktop" in out
    assert out.strip().splitlines()[-2:] == ["exit 0", "False"]
