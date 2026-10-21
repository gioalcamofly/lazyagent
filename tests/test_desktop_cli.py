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
    package_bin = tmp_path / "site-packages" / "lazyagent" / "bin"
    package_bin.mkdir(parents=True)
    on_path_dir = tmp_path / "bin"
    on_path_dir.mkdir()
    explicit = _exe(tmp_path / "explicit")

    def find(env):
        return desktop.find_binary(env, src, package_bin)

    env = {"PATH": str(on_path_dir)}
    assert find(env) is None
    _exe(built)
    assert find(env) == built
    on_path = _exe(on_path_dir / "lazyagent-desktop")
    assert find(env) == on_path
    # The binary a platform wheel ships wins over PATH and the checkout.
    shipped = _exe(package_bin / "lazyagent-desktop")
    assert find(env) == shipped
    with_explicit = {**env, desktop.BIN_ENV: str(explicit)}
    assert find(with_explicit) == explicit
    # An explicit path that is not runnable is an error, not a fallback.
    missing = {**env, desktop.BIN_ENV: str(tmp_path / "nope")}
    assert find(missing) is None


def test_shipped_binary_must_be_executable(tmp_path):
    package_bin = tmp_path / "bin"
    package_bin.mkdir()
    (package_bin / "lazyagent-desktop").write_text("not runnable")
    assert desktop.find_binary({"PATH": ""}, tmp_path, package_bin) is None


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


def test_missing_binary_explains_how_to_build(monkeypatch, capsys):
    """From a checkout: build the checkout's desktop/."""
    monkeypatch.delenv(desktop.BIN_ENV, raising=False)
    monkeypatch.setattr(desktop, "find_binary", lambda: None)
    assert desktop.main([]) == 2
    err = capsys.readouterr().err
    assert "cargo install" in err and str(desktop.SOURCE_ROOT / "desktop/crates/app") in err


def test_pure_wheel_explains_how_to_install(tmp_path):
    """An install from the pure wheel has no desktop/: install it from git."""
    from lazyagent import __version__

    help_text = desktop.build_help(tmp_path)
    assert f"--git {desktop.REPO_URL} --tag v{__version__} lazyagent-desktop" in help_text
    assert "--path" not in help_text


def test_help_says_which_binary_runs(tmp_path, monkeypatch, capsys):
    binary = _exe(tmp_path / "lazyagent-desktop")
    monkeypatch.setattr(desktop, "find_binary", lambda: binary)
    assert desktop.main(["--help"]) == 0
    assert f"app: {binary}" in capsys.readouterr().out
    monkeypatch.setattr(desktop, "find_binary", lambda: None)
    assert desktop.main(["--help"]) == 0
    assert "app: not built" in capsys.readouterr().out


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
