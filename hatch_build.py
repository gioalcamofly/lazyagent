"""Wheel build hook: the desktop app's binary inside a platform wheel.

With ``LAZYAGENT_BUILD_DESKTOP=1`` a (non-editable) wheel build compiles
``desktop/`` with cargo and ships the binary as
``lazyagent/bin/lazyagent-desktop``, in a ``py3-none-<platform>`` wheel: the
binary does not depend on the Python version, so one wheel per platform.
Without the variable the hook does nothing and the wheel stays pure Python
(``uv build``, ``uv sync`` and editable installs need no Rust).

``LAZYAGENT_WHEEL_PLATFORM`` overrides the platform tag (default
``manylinux_2_28_<arch>``, true only when built in that manylinux image; CI
checks it with ``auditwheel show``). ``CARGO_TARGET_DIR`` is honoured.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILD_ENV = "LAZYAGENT_BUILD_DESKTOP"
PLATFORM_ENV = "LAZYAGENT_WHEEL_PLATFORM"
BINARY = "lazyagent-desktop"
WHEEL_PATH = f"lazyagent/bin/{BINARY}"


def wheel_platform(environ=os.environ) -> str:
    explicit = environ.get(PLATFORM_ENV)
    if explicit:
        return explicit
    if sys.platform != "linux":
        raise RuntimeError(
            f"{BUILD_ENV}=1: binary wheels are built for Linux only; "
            f"set {PLATFORM_ENV} to tag another platform"
        )
    return f"manylinux_2_28_{platform.machine()}"


class DesktopBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable" or os.environ.get(BUILD_ENV) != "1":
            return
        manifest = Path(self.root) / "desktop" / "Cargo.toml"
        if not manifest.is_file():
            raise RuntimeError(f"{BUILD_ENV}=1 but {manifest} is missing")
        subprocess.run(
            ["cargo", "build", "--release", "--locked", "--manifest-path", str(manifest)],
            check=True,
        )
        target = Path(os.environ.get("CARGO_TARGET_DIR") or manifest.parent / "target")
        binary = target / "release" / BINARY
        if not binary.is_file():
            raise RuntimeError(f"cargo build left no binary at {binary}")
        binary.chmod(0o755)
        build_data["force_include"][str(binary)] = WHEEL_PATH
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{wheel_platform()}"
