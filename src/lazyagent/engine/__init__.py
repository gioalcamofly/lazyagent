"""The engine: agent and worktree state that does not depend on the UI.

Nothing in this package may import Textual or a widget module
(tests/engine/test_boundary.py). ``Engine`` is exported lazily, so importing
the package alone stays as light as the registry (no pyte).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

__all__ = ["Engine"]


def __getattr__(name: str) -> Any:
    if name == "Engine":
        from lazyagent.engine.engine import Engine

        return Engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
