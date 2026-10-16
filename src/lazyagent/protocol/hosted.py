"""Hosted mode: a host app that runs the TUI inside its own terminal panel.

The host starts ``lazyagent`` with :data:`HOST_ENV` set (its value is opaque
to lazyagent; a name or path the host chooses). The TUI then hides what the
host paints itself and, as its first output, writes a private OSC sequence
that tells the host where the engine's v1 socket is::

    ESC ] 7717 ; lazyagent ; <json> BEL

``<json>`` is one line of ASCII JSON (non-ASCII and control characters are
``\\u`` escaped, so it never contains ESC or BEL)::

    {"v": 1, "socket": "/run/user/1000/lazyagent/<hash>/engine.sock" | null,
     "engine_id": "…" | null, "repo_root": "…", "pid": 1234, "host": "…"}

``socket`` is null when the engine runs without a v1 server. The TUI writes
the sequence again after every terminal resize, so a host that attaches late
(a tmux client) still gets it. Inside tmux it is also written wrapped in a
DCS passthrough (``ESC P tmux; … ESC \\``), which tmux forwards to the outer
terminal when ``allow-passthrough`` is on.

7717 is not used by any terminal we know of (xterm, VTE, kitty, iTerm2,
WezTerm, Windows Terminal use 0-52, 104-119, 133, 633, 777, 1337, 5113, 5522,
9001 and 99).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

HOST_ENV = "LAZYAGENT_HOST"
OSC_NUMBER = 7717
OSC_TAG = "lazyagent"
OSC_VERSION = 1

_OSC_RE = re.compile(
    rb"\x1b\]" + str(OSC_NUMBER).encode() + rb";" + OSC_TAG.encode() + rb";"
    rb"([^\x07\x1b]*)(?:\x07|\x1b\\)"
)


def host(environ: Mapping[str, str]) -> str | None:
    """The host's identifier when the TUI runs hosted, else None."""
    value = environ.get(HOST_ENV, "")
    return value or None


def encode_osc(payload: Mapping[str, Any]) -> str:
    """The announcement sequence for ``payload`` (``v`` is added)."""
    body = json.dumps({"v": OSC_VERSION, **payload}, separators=(",", ":"))
    return f"\x1b]{OSC_NUMBER};{OSC_TAG};{body}\x07"


def tmux_passthrough(sequence: str) -> str:
    """``sequence`` wrapped so tmux hands it to the outer terminal."""
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + "\x1b\\"


def parse_osc(data: bytes) -> list[dict[str, Any]]:
    """Every announcement in a chunk of terminal output, oldest first
    (terminated by BEL or ST). Undecodable ones are skipped."""
    found = []
    for match in _OSC_RE.finditer(data):
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found
