"""The hosted-mode OSC announcement: format, parsing, tmux wrapping."""

from __future__ import annotations

from lazyagent.protocol.hosted import (
    HOST_ENV,
    encode_osc,
    host,
    parse_osc,
    tmux_passthrough,
)


def test_host_reads_the_environment():
    assert host({HOST_ENV: "gui-1"}) == "gui-1"
    assert host({HOST_ENV: ""}) is None
    assert host({}) is None


def test_the_sequence_is_exact():
    assert encode_osc({"socket": "/s", "engine_id": "e"}) == (
        '\x1b]7717;lazyagent;{"v":1,"socket":"/s","engine_id":"e"}\x07'
    )


def test_control_and_non_ascii_characters_are_escaped():
    sequence = encode_osc({"repo_root": "/r\x07\x1bé"})
    body = sequence[len("\x1b]7717;lazyagent;") : -1]
    assert "\x07" not in body and "\x1b" not in body and body.isascii()
    assert parse_osc(sequence.encode()) == [{"v": 1, "repo_root": "/r\x07\x1bé"}]


def test_parse_finds_announcements_among_other_output():
    data = (
        b"\x1b[?1049h\x1b]22;default\x07"
        + encode_osc({"n": 1}).encode()
        + b"hello\x1b]7717;lazyagent;not json\x07"
        + b'\x1b]7717;lazyagent;{"v":1,"n":2}\x1b\\'
        + b'\x1b]7718;lazyagent;{"v":1,"n":3}\x07'
    )
    assert parse_osc(data) == [{"v": 1, "n": 1}, {"v": 1, "n": 2}]


def test_tmux_passthrough_doubles_escapes():
    wrapped = tmux_passthrough(encode_osc({"n": 1}))
    assert wrapped.startswith("\x1bPtmux;\x1b\x1b]7717;")
    assert wrapped.endswith("\x07\x1b\\")
