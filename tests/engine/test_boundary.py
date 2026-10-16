"""lazyagent.engine stays UI-free: no Textual and no widget modules."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import lazyagent
from tests.protocol.test_boundary import _imports

ENGINE = Path(lazyagent.__file__).parent / "engine"

_BANNED = ("textual", "lazyagent.widgets", "lazyagent.app", "lazyagent.messages")


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
