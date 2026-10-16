import pytest

# Some tests build pyte screens or bind pyte.screens.Char before importing any
# lazyagent widget, so apply the patch up front the way the widgets do.
import lazyagent.pyte_patch  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """Keep the persisted agent id counters (and any log) out of the real
    ``~/.local/state``."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
