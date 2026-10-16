"""The host is a v1 client: it loads neither Textual nor pyte."""

from __future__ import annotations

import subprocess
import sys


def test_host_imports_no_textual_or_pyte():
    code = (
        "import sys\n"
        "import lazyagent.host.cli\n"
        "print(any(m == 'textual' or m.startswith('textual.') for m in sys.modules))\n"
        "print('pyte' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    assert out == ["False", "False"]
