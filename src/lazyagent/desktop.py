"""``lazyagent desktop [repo] [options]``: the native desktop app.

The app is a Rust binary (``desktop/`` in the source tree, see
docs/design/desktop-app.md). This command finds it and runs it in place of
this process, with ``LAZYAGENT_PYTHON`` set to this interpreter so the app
starts the repo's engine with the same lazyagent (``python -m lazyagent
engine start``). Options after the repo are the app's (``--theme light``…).

The binary is looked up in this order: ``LAZYAGENT_DESKTOP_BIN``, the one a
platform wheel ships inside the package (``lazyagent/bin/lazyagent-desktop``,
see hatch_build.py), ``lazyagent-desktop`` on PATH, then the release build of
a source checkout (``desktop/target/release/lazyagent-desktop``). Standard
library only; no Textual import.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

BIN_ENV = "LAZYAGENT_DESKTOP_BIN"
BINARY = "lazyagent-desktop"
# Where a platform wheel puts the binary (hatch_build.py).
PACKAGE_BIN = Path(__file__).resolve().parent / "bin"
# src/lazyagent/desktop.py -> the source tree's root.
SOURCE_ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/gioalcamofly/lazyagent"


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
    built = source_root / "desktop" / "target" / "release" / BINARY
    return built if _executable(built) else None


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
    env = dict(os.environ)
    env["LAZYAGENT_PYTHON"] = sys.executable
    os.execve(binary, [str(binary), *argv], env)
    return 1  # not reached
