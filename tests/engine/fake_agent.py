"""Scripted stand-in for an agent CLI, run under a real PTY by session tests.

Prints a banner and a ``your turn>`` prompt. Input is read in raw mode the way
Ink-based CLIs read it: bracketed-paste content is buffered as text, a CR
outside a paste submits, and every read that does not submit redraws the
prompt (which is what acknowledges the input). On submit it clears the
screen, prints ``working``, then ``got: <text> paste=<bool>`` and prompts
again. Submitting ``exit N`` exits with code N. A few more commands script
output for the protocol tests:

- ``count N``: prints ``line 1`` … ``line N``, one per millisecond;
- ``spew N``: prints N bytes of 99-character lines as fast as it can;
- ``size``: prints the terminal size as ``size <rows>x<cols>``.

Each ends with ``done`` and the prompt. A submitted text that asks for an
answer between ``⟦answer ID⟧`` and ``⟦end ID⟧`` lines (the desktop diff
view's questions) gets one after the ``got:`` line, the way Claude Code
prints it (a bullet, then an indent). Every ``[comment_id: ID]`` in a
submitted text, or in the arguments it was started with (a first prompt),
is answered through the engine's ``review_reply`` method, as the
``review_reply`` MCP tool would (``LAZYAGENT_SOCKET`` and
``LAZYAGENT_AGENT_TOKEN`` from the environment), and a ``replied ID`` (or
``reply failed ID: why``) line is printed.

``--paste`` turns bracketed paste (DECSET 2004) on at startup.
``--env=NAME`` prints ``env NAME=<value>`` (``<unset>`` when it is not set)
before the banner, to show which environment the agent was started with.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import tty

START = b"\x1b[200~"
END = b"\x1b[201~"


def out(text: str) -> None:
    os.write(1, text.encode())


def main() -> int:
    tty.setraw(0)
    if "--paste" in sys.argv:
        out("\x1b[?2004h")
    for arg in sys.argv[1:]:
        if arg.startswith("--env="):
            name = arg.removeprefix("--env=")
            out(f"env {name}={os.environ.get(name, '<unset>')}\r\n")
    out("fake agent ready\r\n")
    review_replies(" ".join(sys.argv[1:]))
    out("your turn> ")

    pending = b""
    typed = bytearray()
    in_paste = False
    pasted = False
    while True:
        data = os.read(0, 4096)
        if not data:
            return 0
        pending += data
        submitted = None
        while pending:
            if pending.startswith(START):
                in_paste = pasted = True
                pending = pending[len(START) :]
            elif pending.startswith(END):
                in_paste = False
                pending = pending[len(END) :]
            elif START.startswith(pending) or END.startswith(pending):
                break  # a marker split across reads
            else:
                byte, pending = pending[:1], pending[1:]
                if byte == b"\r" and not in_paste:
                    submitted = typed.decode()
                    typed.clear()
                    break
                typed += byte

        if submitted is None:
            shown = typed.decode(errors="replace").replace("\n", "|")
            out(f"\r\x1b[Kyour turn> {shown}")
            continue

        if submitted.startswith("exit"):
            out("\r\nbye\r\n")
            return int(submitted.split()[1])
        if submitted.startswith(("count ", "spew ", "size")):
            out("\r\n")
            script(submitted)
            out("done\r\nyour turn> ")
            pasted = False
            continue
        out("\x1b[2J\x1b[Hworking\r\n")
        time.sleep(0.3)
        out(f"got: {submitted!r} paste={pasted}\r\n")
        if m := re.search(r"⟦answer (\w+)⟧", submitted):
            out(f"⏺ ⟦answer {m[1]}⟧\r\n  A fake answer\r\n  on two lines.\r\n  ⟦end {m[1]}⟧\r\n")
        review_replies(submitted)
        out("your turn> ")
        pasted = False


def review_replies(text: str) -> None:
    for comment_id in re.findall(r"\[comment_id: ([\w-]+)\]", text):
        try:
            call("review_reply", {
                "comment_id": comment_id,
                "text": f"A fake answer to {comment_id}.",
                "caller": os.environ.get("LAZYAGENT_AGENT_TOKEN", ""),
            })
            out(f"replied {comment_id}\r\n")
        except (KeyError, OSError, RuntimeError) as e:
            out(f"reply failed {comment_id}: {e}\r\n")


def call(method: str, params: dict) -> object:
    """One request to the engine's legacy socket, as the MCP server makes it."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(os.environ["LAZYAGENT_SOCKET"])
        s.sendall(json.dumps({"id": "1", "method": method, "params": params}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    response = json.loads(data)
    if "error" in response:
        raise RuntimeError(response["error"].get("message", ""))
    return response.get("result")


def script(command: str) -> None:
    name, _, arg = command.partition(" ")
    if name == "count":
        for i in range(1, int(arg) + 1):
            out(f"line {i}\r\n")
            time.sleep(0.001)
    elif name == "spew":
        line = b"x" * 97 + b"\r\n"
        left = int(arg)
        while left > 0:
            chunk = (line * 600)[:left]
            os.write(1, chunk)
            left -= len(chunk)
        out("\r\n")
    elif name == "size":
        size = os.get_terminal_size(0)
        out(f"size {size.lines}x{size.columns}\r\n")


if __name__ == "__main__":
    sys.exit(main())
