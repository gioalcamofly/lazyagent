"""``lazyagent host``: a minimal graphical host for the TUI (experimental).

It runs ``lazyagent`` in hosted mode on a PTY and serves a page on
127.0.0.1: the engine's sessions (agents, shells) in native xterm.js
terminals, a sidebar painted from the engine's v1 events, and the TUI in
one more terminal. ``docs/design/engine-split.md`` §6b and
``docs/design/ui-plan.md`` are the design.
"""
