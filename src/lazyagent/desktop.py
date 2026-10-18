"""``lazyagent desktop [repo] [options]``: the native desktop app.

The app is a Rust binary (``desktop/`` in the source tree, see
docs/design/desktop-app.md). This command finds it and runs it in place of
this process, with ``LAZYAGENT_PYTHON`` set to this interpreter so the app
starts the repo's engine with the same lazyagent (``python -m lazyagent
engine start``). Options after the repo are the app's (``--theme light``…).

The binary is looked up in this order: ``LAZYAGENT_DESKTOP_BIN``,
``lazyagent-desktop`` on PATH, then the release build of a source checkout
(``desktop/target/release/lazyagent-desktop``). Standard library only; no
Textual import.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

BIN_ENV = "LAZYAGENT_DESKTOP_BIN"
BINARY = "lazyagent-desktop"
# src/lazyagent/desktop.py -> the source tree's root.
SOURCE_ROOT = Path(__file__).resolve().parents[2]

BUILD_HELP = f"""\
lazyagent desktop: the desktop app is not built.

It is a Rust program in the lazyagent source tree (desktop/). With a Rust
toolchain (https://rustup.rs):

    cargo install --locked --path {SOURCE_ROOT / "desktop/crates/app"}

or build it in place:

    cargo build --release --manifest-path {SOURCE_ROOT / "desktop/Cargo.toml"}

Or point {BIN_ENV} at a binary.
"""


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def find_binary(
    environ: Mapping[str, str] = os.environ, source_root: Path = SOURCE_ROOT
) -> Path | None:
    """The desktop binary to run, or None when there is none."""
    explicit = environ.get(BIN_ENV)
    if explicit:
        return Path(explicit) if _executable(Path(explicit)) else None
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
            "runs). App options: lazyagent-desktop --help."
        )
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
            print(BUILD_HELP, file=sys.stderr, end="")
        return 2
    env = dict(os.environ)
    env["LAZYAGENT_PYTHON"] = sys.executable
    os.execve(binary, [str(binary), *argv], env)
    return 1  # not reached
