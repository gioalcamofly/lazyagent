"""lazyagent.protocol stays standalone: stdlib only, no Textual, no other
lazyagent modules, and only the v1 server and client use it."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import lazyagent

PACKAGE = Path(lazyagent.__file__).parent
PROTOCOL = PACKAGE / "protocol"


def _imports(path: Path) -> list[tuple[str, int]]:
    """(absolute module, relative level) for every import in a file."""
    found = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


def test_protocol_imports_only_stdlib_and_itself():
    stdlib = sys.stdlib_module_names
    offenders = []
    for path in sorted(PROTOCOL.rglob("*.py")):
        for module, level in _imports(path):
            if level > 0:
                # Relative imports must stay inside the package.
                if level > len(path.relative_to(PROTOCOL).parts):
                    offenders.append(f"{path.name}: {'.' * level}{module}")
            elif module.split(".")[0] not in stdlib and module != "__future__":
                offenders.append(f"{path.name}: {module}")
    assert offenders == []


def test_importing_protocol_loads_no_textual_or_app_modules():
    code = (
        "import sys\n"
        "import lazyagent.protocol, lazyagent.protocol.framing, "
        "lazyagent.protocol.handshake, lazyagent.protocol.validation\n"
        "mods = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('textual', 'lazyagent', 'mcp', 'pyte'))\n"
        "print('\\n'.join(mods))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    unexpected = [m for m in out if m != "lazyagent" and not m.startswith("lazyagent.protocol")]
    assert unexpected == []


def test_the_pyte_patch_is_applied_wherever_pyte_screens_are_built():
    # The widgets and the MCP server entry point are the paths that run in
    # production; the patch must be in place after importing either.
    code = (
        "import sys\n"
        "import lazyagent.mcp_server\n"
        "print('pyte' in sys.modules)\n"
        "import lazyagent.widgets.scrollable_terminal\n"
        "from pyte.screens import Char\n"
        "print('dim' in Char._fields)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["False", "True"]


def test_only_the_v1_server_and_client_import_protocol():
    users = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        if PROTOCOL in path.parents:
            continue
        for module, _ in _imports(path):
            if module.startswith("lazyagent.protocol"):
                users.add(str(path.relative_to(PACKAGE)))
    assert users == {"engine/server.py", "engine/client.py"}


def test_schemas_ship_inside_the_package():
    assert sorted(p.name for p in (PROTOCOL / "schema" / "v1").glob("*.json")) == [
        "envelope.json", "events.json", "handshake.json", "methods.json",
    ]
