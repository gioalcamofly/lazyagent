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


# ----------------------------------------------------------------------
# a source checkout's build older than its sources
# ----------------------------------------------------------------------


def _age(path, seconds):
    when = path.stat().st_mtime - seconds
    os.utime(path, (when, when))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A checkout's desktop/ with a release build newer than its sources,
    and a fake cargo on PATH that records how it ran (no real build)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "checkout"
    desk = root / "desktop"
    for name in (
        "Cargo.toml",
        "Cargo.lock",
        "crates/app/Cargo.toml",
        "crates/app/build.rs",
        "crates/app/src/main.rs",
        "crates/proto/src/lib.rs",
        "crates/proto/tests/wire.rs",
    ):
        (desk / name).parent.mkdir(parents=True, exist_ok=True)
        (desk / name).write_text("")
        _age(desk / name, 120)
    binary = desktop.checkout_build(root)
    binary.parent.mkdir(parents=True)
    _exe(binary)
    _age(binary, 60)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "cargo.calls"
    _exe(bin_dir / "cargo").write_text(
        f"#!/bin/sh\necho \"$PWD $*\" >> {calls}\nexit ${{FAKE_CARGO_EXIT:-0}}\n"
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    class Tree:
        pass

    t = Tree()
    t.root, t.desktop, t.binary = root, desk, binary
    t.calls = lambda: calls.read_text().splitlines() if calls.exists() else []
    return t


def test_a_build_is_stale_when_a_source_is_newer(tree):
    assert not desktop.is_stale(tree.binary, tree.root)
    for name in ("crates/proto/src/lib.rs", "Cargo.lock", "crates/app/build.rs"):
        (tree.desktop / name).touch()
        assert desktop.is_stale(tree.binary, tree.root), name
        _age(tree.desktop / name, 120)
    # A crate's tests are not in the binary.
    (tree.desktop / "crates/proto/tests/wire.rs").touch()
    assert not desktop.is_stale(tree.binary, tree.root)
    assert not desktop.is_stale(tree.root / "missing", tree.root)


def test_a_stale_build_is_rebuilt_before_it_runs(tree, capsys):
    (tree.desktop / "crates/app/src/main.rs").touch()
    desktop.rebuild_if_stale(tree.binary, tree.root)
    assert tree.calls() == [f"{tree.root} {' '.join(desktop.RELEASE_BUILD)}"]
    assert "building it (cargo build --release" in capsys.readouterr().err
    # The fake cargo built nothing (cargo's answer when nothing changed):
    # the binary is marked built, so the next launch does not build again.
    assert not desktop.is_stale(tree.binary, tree.root)
    desktop.rebuild_if_stale(tree.binary, tree.root)
    assert len(tree.calls()) == 1
    assert capsys.readouterr().err == ""


def test_a_failed_build_warns_and_the_old_build_runs(tree, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CARGO_EXIT", "101")
    (tree.desktop / "crates/app/src/main.rs").touch()
    monkeypatch.setattr(desktop, "find_binary", lambda: tree.binary)
    monkeypatch.setattr(desktop, "SOURCE_ROOT", tree.root)
    calls = []
    monkeypatch.setattr(os, "execve", lambda path, args, env: calls.append(path))
    desktop.main(["/some/repo"])
    err = capsys.readouterr().err
    assert "the build failed (exit 101)" in err and "cargo build --release" in err
    assert calls == [tree.binary]
    assert desktop.is_stale(tree.binary, tree.root), "still stale: the next launch tries again"


def test_without_cargo_a_stale_build_warns(tree, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/nonexistent")
    (tree.desktop / "Cargo.lock").touch()
    desktop.rebuild_if_stale(tree.binary, tree.root)
    err = capsys.readouterr().err
    assert "warning" in err and "cargo is not on PATH" in err
    assert f"cargo {' '.join(desktop.RELEASE_BUILD)}" in err
    assert tree.calls() == []


def test_only_the_checkout_build_is_checked(tree, tmp_path, capsys):
    """A wheel's, PATH's or LAZYAGENT_DESKTOP_BIN's binary is never rebuilt."""
    (tree.desktop / "crates/app/src/main.rs").touch()
    shipped = tmp_path / "site-packages" / "lazyagent" / "bin"
    shipped.mkdir(parents=True)
    other = _exe(shipped / "lazyagent-desktop")
    _age(other, 600)
    desktop.rebuild_if_stale(other, tree.root)
    assert tree.calls() == [] and capsys.readouterr().err == ""
