import itertools
import json
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
    # Nor read the user's global config (trackers.toml).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Nor the user's agent prompts (lazyagent prompts): an empty HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


@pytest.fixture(autouse=True)
def _no_real_github(monkeypatch):
    """GitHub Actions polling never reaches the real ``gh``: a test gives
    the hub its own runner."""
    from lazyagent.engine import actions

    def runner(cwd):
        async def run(args):
            raise actions.TrackerError("tests never call the real gh")

        return run

    monkeypatch.setattr(actions, "_gh_runner", runner)


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


@pytest.fixture(params=["inprocess", "remote"])
def backend_kind(request):
    """Which backend a TUI pilot test runs against (see tests/tui_backends.py)."""
    return request.param


@pytest.fixture
def record(request, capsys):
    """``record(name, **numbers)``: print a perf result and keep it in
    ``.perf/results.jsonl`` (tests/perf/stats.py)."""
    from tests.perf.stats import keep

    def write(name: str, **numbers) -> None:
        keep(request.node.nodeid, name, numbers)
        with capsys.disabled():
            print(f"\nPERF {name}: {json.dumps(numbers)}")

    return write


@pytest.fixture(autouse=True)
def _no_agent_identity(monkeypatch):
    """Run inside a lazyagent agent, the suite inherits its identity (token,
    socket, notes path); the agents a test spawns must not, and no MCP
    server under test may reach the live engine."""
    from lazyagent.agent_providers import IDENTITY_ENV

    for name in IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)
