import itertools
import shutil
import tempfile

import pytest

# Some tests build pyte screens or bind pyte.screens.Char before importing any
# lazyagent widget, so apply the patch up front the way the widgets do.
import lazyagent.pyte_patch  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Keep the persisted agent id counters (and any log) out of the real
    ``~/.local/state``."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _temp_dirs_in_tmp_path(tmp_path, monkeypatch):
    """Agent runtime contexts (hook scripts, settings, MCP configs) are
    ``mkdtemp`` dirs; keep a test's under its ``tmp_path``, not in /tmp."""
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(suffix=None, prefix=None, dir=None):
        return real_mkdtemp(suffix, prefix, tmp_path if dir is None else dir)

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)


@pytest.fixture(scope="session")
def _runtime_base():
    # Not under tmp_path: a Unix socket path must fit in 108 bytes.
    base = tempfile.mkdtemp(prefix="lzr-")
    try:
        yield base, itertools.count(1)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def runtime_dir(_runtime_base, monkeypatch):
    """A private, short ``$XDG_RUNTIME_DIR`` per test, so the engine's v1
    socket and ``engine.json`` never land in the real runtime dir."""
    base, counter = _runtime_base
    path = f"{base}/{next(counter)}"
    monkeypatch.setenv("XDG_RUNTIME_DIR", path)
    return path
