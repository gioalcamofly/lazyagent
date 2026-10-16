"""Where an engine's v1 socket lives, computable from the repo root alone.

Standard library only, so the reference client can find an engine without
importing the server. ``docs/design/engine-split.md`` §4f is the spec.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SOCKET_NAME = "engine.sock"
INFO_NAME = "engine.json"


def runtime_dir(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    """The per-repo runtime dir (§4f)."""
    digest = hashlib.sha1(repo_root.encode()).hexdigest()[:12]
    base = environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "lazyagent" / digest
    return Path(tempfile.gettempdir()) / f"lazyagent-{os.getuid()}" / digest


def socket_path(repo_root: str, environ: Mapping[str, str] = os.environ) -> Path:
    return runtime_dir(repo_root, environ) / SOCKET_NAME


def read_info(path: Path) -> dict[str, Any] | None:
    """``engine.json``, or None when missing or unreadable."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
