#!/usr/bin/env python3
"""Claude Code hook for an agent's session notes (proposal 0011, N3).

Copied next to log_hook.py into each agent's hooks dir and run by Claude,
never imported by lazyagent: stdlib only, so it works whatever Python and
install the agent runs with. ``LAZYAGENT_NOTES_PATH`` is the agent's
``<id>.md``; the engine writes it and ``<id>.json`` (the meta), this script
writes only ``<id>.seen.json``: what the agent last saw.

- ``SessionStart`` (compact, clear, resume; startup too, a restore may
  report it): the notes as ``additionalContext``, when not empty.
- ``UserPromptSubmit``: when someone else wrote them since the agent last
  saw them, "notes updated" and a short diff.

It always exits 0 and prints nothing on any error: broken notes must never
block a prompt.
"""

import difflib
import json
import os
import re
import sys
import time
from pathlib import Path

NOTES_PATH_ENV = "LAZYAGENT_NOTES_PATH"
_HEADING = re.compile(r"^##[ \t]+(.*?)\s*$")
SOURCES = ("startup", "resume", "clear", "compact")
DIFF_LIMIT = 1536


def _read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_empty(text):
    """Only ``##`` headings (the template): nothing to show. As
    ``lazyagent.engine.notes.is_empty``."""
    return all(_HEADING.match(line) for line in text.splitlines() if line.strip())


def _write_seen(path, rev, text):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({"rev": rev, "text": text}).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _when(stamp):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(stamp)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _header(meta):
    by = meta.get("by") or "nobody yet"
    if meta.get("mode") == "free":
        rule = "keep them current"
    else:
        rule = "write to them only when the user asks"
    lines = [
        f"Session notes (lazyagent; last edit {_when(meta.get('updated_at'))} "
        f"by {by}; edit with the notes_* tools, {rule}):"
    ]
    instruction = meta.get("instruction")
    if isinstance(instruction, str) and instruction.strip():
        lines += ["", "Original instruction (pinned):", instruction.strip()]
    return "\n".join(lines)


def _output(event, context):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": context,
                }
            }
        )
    )


def _diff(old, new):
    lines = difflib.unified_diff(
        old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=1
    )
    text = "\n".join(lines)
    if len(text.encode()) <= DIFF_LIMIT:
        return text
    clipped = text.encode()[:DIFF_LIMIT].decode(errors="ignore")
    return clipped.rpartition("\n")[0] + "\n… (diff clipped: read them with notes_read)"


def run(payload, environ):
    """What to print for one hook payload, or None."""
    notes_path = environ.get(NOTES_PATH_ENV)
    if not notes_path or not isinstance(payload, dict):
        return None
    if payload.get("agent_id"):
        return None  # a subagent's event
    md = Path(notes_path)
    meta_path = md.with_suffix(".json")
    seen_path = md.with_name(md.stem + ".seen.json")
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "UserPromptSubmit"):
        return None
    # The meta first: the engine writes the .md before it, so a text newer
    # than the meta's rev shows up again (as an empty diff) next time.
    meta = _read_json(meta_path)
    if meta is None or not isinstance(meta.get("rev"), int):
        return None
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    rev = meta["rev"]

    if event == "SessionStart":
        if payload.get("source") not in SOURCES or _is_empty(text):
            return None
        _write_seen(seen_path, rev, text)
        return event, f"{_header(meta)}\n\n{text.strip()}"

    seen = _read_json(seen_path) or {}
    seen_rev = seen.get("rev") if isinstance(seen.get("rev"), int) else 0
    seen_text = seen.get("text") if isinstance(seen.get("text"), str) else ""
    if rev <= seen_rev:
        return None
    _write_seen(seen_path, rev, text)
    if meta.get("by") in (None, "agent") or text == seen_text:
        return None
    diff = _diff(seen_text, text)
    by = meta.get("by")
    return event, (
        f"Your session notes were updated by {by} "
        f"({_when(meta.get('updated_at'))}):\n{diff}"
    )


def main():
    try:
        payload = json.loads(sys.stdin.read() or "null")
        result = run(payload, os.environ)
        if result is not None:
            _output(*result)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
