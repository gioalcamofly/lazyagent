"""Wheel build hook: ``lazyagent/_version.py``, and the desktop app's binary
inside a platform wheel.

Every wheel gets ``lazyagent/_version.py`` (``__version__``, read instead of
``importlib.metadata``, which costs most of the package's import). A wheel
gets it from a temp file, never written into the tree: the platform wheels
build from a read-only mount. An editable install reads the tree, so there
it is written to ``src/lazyagent/_version.py`` (gitignored), when it can be.

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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

BUILD_ENV = "LAZYAGENT_BUILD_DESKTOP"
PLATFORM_ENV = "LAZYAGENT_WHEEL_PLATFORM"
BINARY = "lazyagent-desktop"
WHEEL_PATH = f"lazyagent/bin/{BINARY}"
VERSION_PATH = "lazyagent/_version.py"


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

    _version_dir: str | None = None

    def initialize(self, version: str, build_data: dict) -> None:
        self._version_file(version, build_data)
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

    def _version_file(self, version: str, build_data: dict) -> None:
        source = f'__version__ = "{self.metadata.version}"\n'
        if version == "editable":
            path = Path(self.root) / "src" / VERSION_PATH
            try:
                if not path.is_file() or path.read_text() != source:
                    path.write_text(source)
            except OSError:
                pass  # lazyagent/__init__.py falls back to importlib.metadata
            return
        self._version_dir = tempfile.mkdtemp(prefix="lazyagent-version-")
        path = Path(self._version_dir) / "_version.py"
        path.write_text(source)
        build_data["force_include"][str(path)] = VERSION_PATH

    def finalize(self, version: str, build_data: dict, artifact_path: str) -> None:
        if self._version_dir is not None:
            shutil.rmtree(self._version_dir, ignore_errors=True)
            self._version_dir = None
