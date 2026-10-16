"""lazyagent.protocol stays standalone: stdlib only, no Textual, no other
lazyagent modules, and nothing in the app uses it yet."""

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
        "('textual', 'lazyagent', 'mcp'))\n"
        "print('\\n'.join(mods))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    # lazyagent/__init__ imports pyte_patch; that is the package's own
    # concern, not the protocol's.
    allowed = {"lazyagent", "lazyagent.pyte_patch"}
    unexpected = [m for m in out if m not in allowed and not m.startswith("lazyagent.protocol")]
    assert unexpected == []


def test_nothing_imports_protocol_yet():
    users = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if PROTOCOL in path.parents:
            continue
        for module, _ in _imports(path):
            if module.startswith("lazyagent.protocol"):
                users.append(str(path.relative_to(PACKAGE)))
    assert users == []


def test_schemas_ship_inside_the_package():
    assert sorted(p.name for p in (PROTOCOL / "schema" / "v1").glob("*.json")) == [
        "envelope.json", "events.json", "handshake.json", "methods.json",
    ]
