"""Answer a tool approval from Home (proposal 0013, I4e): which keys answer
the permission prompt on an agent's screen.

The engine only writes a key when the prompt is on the live screen right
now: the agent's status alone (``waiting_for_approval``) can lag behind the
screen, and a key sent to anything else would be typed into the agent's
prompt. :func:`prompt_keys` is pure: screen text in, keys out.

- **Claude Code**'s permission menu: a "Do you want to …?" line, then
  numbered options from ``1. Yes`` to one starting with ``No``. Allow is
  ``1``; deny is Esc (the menu's "No, and tell Claude what to do
  differently").
- **Codex**-style approvals: an option line with ``Yes`` and a ``(y)`` key
  hint. Allow is ``y``; deny is Esc.

Anything else is None: the user answers in the agent's terminal.
"""

from __future__ import annotations

import re

ESC = "\x1b"

_QUESTION = re.compile(r"\bDo you want to\b.*\?")
# "❯ 1. Yes", "  2. Yes, and don't ask again", "  3. No, and tell Claude …"
_OPTION = re.compile(r"^[\s│|❯›>▶▸*]*(\d+)[.)]\s+(.*)$")
_CODEX_YES = re.compile(r"\bYes\b.*\(y\)", re.IGNORECASE)


def _claude(lines: list[str]) -> bool:
    asked = [i for i, line in enumerate(lines) if _QUESTION.search(line)]
    if not asked:
        return False
    options = []
    for line in lines[asked[-1] + 1:]:
        found = _OPTION.match(line)
        if found is not None:
            options.append((found.group(1), found.group(2).strip()))
    if not options or options[0][0] != "1" or not options[0][1].startswith("Yes"):
        return False
    return any(text.startswith("No") for _, text in options[1:])


def _codex(lines: list[str]) -> bool:
    return any(_CODEX_YES.search(line) for line in lines)


def prompt_keys(screen_text: str) -> dict[str, str] | None:
    """``{"allow": key, "deny": key}`` for the permission prompt on
    ``screen_text`` (an agent's live screen), or None when none is there."""
    lines = [line.rstrip() for line in screen_text.splitlines()]
    if _codex(lines):
        return {"allow": "y", "deny": ESC}
    if _claude(lines):
        return {"allow": "1", "deny": ESC}
    return None
