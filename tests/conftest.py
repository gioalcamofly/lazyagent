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
