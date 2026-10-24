import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Nothing here reads or writes the real home: HOME and every XDG dir
    point under the test's tmp_path (the root conftest covers state and
    config)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
