"""lazyagent.engine stays UI-free: no Textual and no widget modules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lazyagent
from tests.protocol.test_boundary import _imports

ENGINE = Path(lazyagent.__file__).parent / "engine"

_BANNED = ("textual", "lazyagent.widgets", "lazyagent.app")


def _banned(module: str) -> bool:
    return any(module == b or module.startswith(b + ".") for b in _BANNED)


def test_engine_imports_no_textual_or_widgets():
    offenders = [
        f"{path.name}: {module}"
        for path in sorted(ENGINE.rglob("*.py"))
        for module, level in _imports(path)
        if level == 0 and _banned(module)
    ]
    assert offenders == []


def test_importing_engine_loads_no_textual_or_widgets():
    code = (
        "import sys\n"
        "import lazyagent.engine, lazyagent.engine.registry, lazyagent.engine.worktrees\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert [m for m in out if _banned(m) or m.split(".")[0] == "pyte"] == []


def test_importing_the_session_loads_no_textual_or_widgets():
    """The session owns the headless pyte screen, so pyte is allowed here."""
    code = (
        "import sys\n"
        "import lazyagent.engine.session, lazyagent.engine.screen\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "pyte" in out
    assert [m for m in out if _banned(m) or m.split(".")[0] == "rich"] == []


def test_importing_the_whole_engine_and_ipc_loads_no_textual_or_widgets():
    """The facade, every engine module and the IPC server it starts are UI-free:
    the IPC server runs without the Textual app."""
    modules = sorted(
        "lazyagent.engine." + path.stem
        for path in ENGINE.glob("*.py")
        # __main__ runs an engine when imported.
        if path.stem not in ("__init__", "__main__")
    )
    code = (
        "import sys, importlib\n"
        "from lazyagent.engine import Engine\n"
        f"for name in {modules!r} + ['lazyagent.ipc']:\n"
        "    importlib.import_module(name)\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert "lazyagent.engine.engine" in out and "lazyagent.ipc" in out
    assert [m for m in out if _banned(m)] == []


def _third_party(path: Path) -> list[str]:
    stdlib = sys.stdlib_module_names
    return [
        module
        for module, level in _imports(path)
        if level == 0
        and module.split(".")[0] not in stdlib
        and module.split(".")[0] not in ("lazyagent", "__future__")
    ]


def test_the_v1_server_and_client_use_only_the_stdlib():
    assert _third_party(ENGINE / "server.py") == []
    assert _third_party(ENGINE / "client.py") == []
    assert _third_party(ENGINE / "runtime.py") == []


def test_the_client_needs_only_the_protocol_and_the_runtime_dir():
    code = (
        "import sys\n"
        "import lazyagent.engine.client\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    lazy = sorted(m for m in out if m.split(".")[0] == "lazyagent")
    assert all(
        m
        in (
            "lazyagent",
            "lazyagent._version",
            "lazyagent.engine",
            "lazyagent.engine.client",
            "lazyagent.engine.runtime",
        )
        or m.startswith("lazyagent.protocol")
        for m in lazy
    ), lazy
    assert [m for m in out if m.split(".")[0] in ("pyte", "textual", "rich")] == []
