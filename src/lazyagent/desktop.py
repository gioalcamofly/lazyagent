"""``lazyagent desktop [repo] [options]``: the native desktop app.

The app is a Rust binary (``desktop/`` in the source tree, see
docs/design/desktop-app.md). This command finds it and runs it in place of
this process, with ``LAZYAGENT_PYTHON`` set to this interpreter so the app
starts the repo's engine with the same lazyagent (``python -m lazyagent
engine start``). Options after the repo are the app's (``--theme light``…).

The binary is looked up in this order: ``LAZYAGENT_DESKTOP_BIN``, the one a
platform wheel ships inside the package (``lazyagent/bin/lazyagent-desktop``,
see hatch_build.py), ``lazyagent-desktop`` on PATH, then the release build of
a source checkout (``desktop/target/release/lazyagent-desktop``).

That last one is rebuilt first when it is older than the sources it is built
from (a pull or an edit since the last ``cargo build``): ``cargo build
--release`` runs in the terminal, and when cargo is missing or the build
fails, a warning names the command and the previous build runs. The test is
modification times (``is_stale``), not commits: no git call on every launch,
uncommitted edits count, and it is the same test cargo makes. Standard
library only; no Textual import.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

BIN_ENV = "LAZYAGENT_DESKTOP_BIN"
BINARY = "lazyagent-desktop"
# Where a platform wheel puts the binary (hatch_build.py).
PACKAGE_BIN = Path(__file__).resolve().parent / "bin"
# src/lazyagent/desktop.py -> the source tree's root.
SOURCE_ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/gioalcamofly/lazyagent"
# The checkout's release build, run from its root (lazyagent update runs it too).
RELEASE_BUILD = ("build", "--release", "--locked", "--manifest-path", "desktop/Cargo.toml")


def build_help(source_root: Path = SOURCE_ROOT) -> str:
    """How to get the binary: build the checkout, or install it from git."""
    if (source_root / "desktop" / "Cargo.toml").is_file():
        how = f"""\
It is a Rust program in the lazyagent source tree (desktop/). With a Rust
toolchain (https://rustup.rs):

    cargo install --locked --path {source_root / "desktop/crates/app"}

or build it in place:

    cargo build --release --manifest-path {source_root / "desktop/Cargo.toml"}
"""
    else:
        from lazyagent import __version__

        how = f"""\
This lazyagent install has no app binary: the published wheels carry one
for Linux x86_64 and aarch64 only, and this platform got the pure Python
one. With a Rust toolchain (https://rustup.rs), build the matching version:

    cargo install --locked --git {REPO_URL} --tag v{__version__} lazyagent-desktop
"""
    return (
        "lazyagent desktop: the desktop app is not built.\n\n"
        f"{how}\nOr point {BIN_ENV} at a binary.\n"
    )


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_binary(
    environ: Mapping[str, str] = os.environ,
    source_root: Path = SOURCE_ROOT,
    package_bin: Path = PACKAGE_BIN,
) -> Path | None:
    """The desktop binary to run, or None when there is none."""
    explicit = environ.get(BIN_ENV)
    if explicit:
        return Path(explicit) if _executable(Path(explicit)) else None
    shipped = package_bin / BINARY
    if _executable(shipped):
        return shipped
    on_path = shutil.which(BINARY, path=environ.get("PATH"))
    if on_path:
        return Path(on_path)
    built = checkout_build(source_root)
    return built if _executable(built) else None


def checkout_build(source_root: Path = SOURCE_ROOT) -> Path:
    """Where ``RELEASE_BUILD`` puts the binary."""
    return source_root / "desktop" / "target" / "release" / BINARY


def _sources(desktop: Path) -> Iterator[Path]:
    """The files a release build of ``desktop/`` is made from."""
    yield desktop / "Cargo.toml"
    yield desktop / "Cargo.lock"
    for crate in sorted((desktop / "crates").glob("*/")):
        yield crate / "Cargo.toml"
        yield crate / "build.rs"
        yield from (crate / "src").rglob("*")


def is_stale(binary: Path, source_root: Path = SOURCE_ROOT) -> bool:
    """Whether a source file of the checkout's app is newer than ``binary``."""
    try:
        built = binary.stat().st_mtime
    except OSError:
        return False
    for path in _sources(source_root / "desktop"):
        try:
            if path.is_file() and path.stat().st_mtime > built:
                return True
        except OSError:
            continue
    return False


def cargo() -> str | None:
    """``cargo`` on PATH, else rustup's default place."""
    found = shutil.which("cargo")
    if found:
        return found
    default = Path.home() / ".cargo" / "bin" / "cargo"
    return str(default) if _executable(default) else None


def rebuild_if_stale(binary: Path, source_root: Path = SOURCE_ROOT) -> None:
    """Rebuild the checkout's app when it predates its sources; else, or
    when that fails, say how. The binary runs either way."""
    if binary != checkout_build(source_root) or not is_stale(binary, source_root):
        return
    command = f"cargo {' '.join(RELEASE_BUILD)}"
    tool = cargo()
    if tool is None:
        print(
            "lazyagent desktop: warning: the app is older than its sources in "
            f"{source_root / 'desktop'}, and cargo is not on PATH. Running the "
            f"old build; to update it, run in {source_root}:\n    {command}",
            file=sys.stderr,
        )
        return
    print(
        "lazyagent desktop: the app is older than its sources; building it "
        f"({command})…",
        file=sys.stderr,
        flush=True,
    )
    try:
        code = subprocess.run([tool, *RELEASE_BUILD], cwd=source_root, check=False).returncode
    except OSError as e:
        code, reason = 127, str(e)
    else:
        reason = f"exit {code}"
    if code != 0:
        print(
            f"lazyagent desktop: warning: the build failed ({reason}); running "
            f"the previous build. Fix it and run in {source_root}:\n    {command}",
            file=sys.stderr,
        )
        return
    mark_built(binary, source_root)


def mark_built(binary: Path, source_root: Path = SOURCE_ROOT) -> None:
    """After a build that succeeded: when cargo found nothing to build (a
    file touched, not changed, or one the app does not use), the binary
    stays older than that file; note that the sources were built."""
    if binary == checkout_build(source_root) and is_stale(binary, source_root):
        os.utime(binary)


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(
            "usage: lazyagent desktop [repo] [app options]\n\n"
            "Run the native desktop app on the repo's engine (started when none\n"
            "runs). `lazyagent desktop --install` adds it to the application\n"
            "menu. App options: lazyagent-desktop --help.\n"
        )
        binary = find_binary()
        print(f"app: {binary}" if binary else "app: not built (run `lazyagent desktop`)")
        return 0
    binary = find_binary()
    if binary is None:
        explicit = os.environ.get(BIN_ENV)
        if explicit:
            print(
                f"lazyagent desktop: {BIN_ENV}={explicit} is not an executable file",
                file=sys.stderr,
            )
        else:
            print(build_help(), file=sys.stderr, end="")
        return 2
    rebuild_if_stale(binary, SOURCE_ROOT)
    env = dict(os.environ)
    env["LAZYAGENT_PYTHON"] = sys.executable
    os.execve(binary, [str(binary), *argv], env)
    return 1  # not reached
