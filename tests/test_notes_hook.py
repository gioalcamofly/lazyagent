"""The notes hook (notes_hook.py) as Claude runs it: a subprocess fed hook
payloads on stdin, with ``LAZYAGENT_NOTES_PATH`` in its env, reading
files the real store wrote."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lazyagent.agent_providers import NOTES_PATH_ENV
from lazyagent.engine.notes import NotesStore
from lazyagent.notes_hook import DIFF_LIMIT
from tests.bench import factor

SCRIPT = Path(__file__).parents[1] / "src" / "lazyagent" / "notes_hook.py"


@pytest.fixture
def store(tmp_path):
    return NotesStore(tmp_path / "notes", clock=lambda: 1_760_000_000.0)


@pytest.fixture
def notes(store):
    """Live notes of an agent; the files on disk."""
    return store.create("/repo-feat", "a1").notes_id


def _run(
    payload, store: NotesStore | None = None, notes_id: str | None = None, env=None
):
    environ = {k: v for k, v in os.environ.items() if k != NOTES_PATH_ENV}
    if store is not None and notes_id is not None:
        environ[NOTES_PATH_ENV] = str(store.path(notes_id))
    environ.update(env or {})
    data = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=data if isinstance(data, bytes) else data.encode(),
        capture_output=True,
        env=environ,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stderr == b""
    return result.stdout.decode()


def _context(stdout: str, event: str) -> str:
    out = json.loads(stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == event
    return out["hookSpecificOutput"]["additionalContext"]


def _start(source: str) -> dict:
    return {"hook_event_name": "SessionStart", "source": source, "session_id": "c1"}


PROMPT = {"hook_event_name": "UserPromptSubmit", "prompt": "go on", "session_id": "c1"}


@pytest.mark.parametrize("source", ["compact", "clear", "resume", "startup"])
def test_session_start_injects_the_notes(store, notes, source):
    store.append(notes, "Decisions", "use sqlite", by="agent")
    store.set_instruction(notes, "Build the importer")
    context = _context(_run(_start(source), store, notes), "SessionStart")
    header, _, body = context.partition("\n\n")
    assert header.startswith("Session notes (lazyagent; last edit ")
    assert "by agent; edit with the notes_* tools" in header
    assert "write to them only when the user asks" in header
    assert "Original instruction (pinned):\nBuild the importer" in context
    assert "## Decisions\n- use sqlite" in context
    assert context.endswith(store.get(notes).text.strip())


def test_free_mode_asks_to_keep_them_current(store):
    notes = store.create("/repo-feat", "a1", mode="free").notes_id
    store.append(notes, "Goal", "ship it", by="orchestrator")
    context = _context(_run(_start("compact"), store, notes), "SessionStart")
    assert "by orchestrator" in context and "keep them current" in context


@pytest.mark.parametrize("source", ["compact", "startup"])
def test_template_only_notes_inject_nothing(store, notes, source):
    store.set_instruction(notes, "Build the importer")
    assert _run(_start(source), store, notes) == ""


def test_an_unknown_source_or_event_injects_nothing(store, notes):
    store.append(notes, "Decisions", "use sqlite", by="agent")
    assert _run(_start("teleport"), store, notes) == ""
    assert _run({"hook_event_name": "Stop"}, store, notes) == ""
    assert _run({**_start("compact"), "agent_id": "sub-1"}, store, notes) == ""


@pytest.mark.parametrize(
    "payload", ["", "not json", "[1, 2]", b"\xff\xfe", json.dumps(PROMPT)[:10]]
)
def test_garbage_payloads_are_ignored(store, notes, payload):
    store.append(notes, "Decisions", "use sqlite", by="user")
    assert _run(payload, store, notes) == ""


def test_missing_env_or_files_print_nothing(store, notes, tmp_path):
    store.append(notes, "Decisions", "use sqlite", by="user")
    assert _run(_start("compact")) == ""
    assert _run(PROMPT) == ""
    gone = {NOTES_PATH_ENV: str(tmp_path / "gone" / "0123456789ab.md")}
    assert _run(_start("compact"), env=gone) == ""
    assert _run(PROMPT, env=gone) == ""
    store.path(notes).unlink()
    assert _run(_start("compact"), store, notes) == ""


@pytest.mark.parametrize("meta", ["{corrupt", "[]", '{"rev": "1"}', ""])
def test_a_corrupt_meta_prints_nothing(store, notes, meta):
    store.append(notes, "Decisions", "use sqlite", by="user")
    store.path(notes).with_suffix(".json").write_text(meta)
    assert _run(_start("compact"), store, notes) == ""
    assert _run(PROMPT, store, notes) == ""


def test_a_user_edit_is_noticed_once(store, notes):
    store.append(notes, "Decisions", "use sqlite", by="agent")
    _context(_run(_start("startup"), store, notes), "SessionStart")  # seen: rev 1
    store.append(notes, "Constraints (don't touch)", "the CLI", by="user")
    context = _context(_run(PROMPT, store, notes), "UserPromptSubmit")
    assert context.startswith("Your session notes were updated by user (")
    assert "+- the CLI" in context and "-- use sqlite" not in context
    assert _run(PROMPT, store, notes) == ""  # seen now
    seen = json.loads(store.path(notes).with_name(f"{notes}.seen.json").read_text())
    assert seen == {"rev": 2, "text": store.get(notes).text}


def test_the_first_prompt_after_a_user_edit_shows_it_all(store, notes):
    store.put(notes, "## Goal\n- ship the importer\n", if_rev=0, by="user")
    context = _context(_run(PROMPT, store, notes), "UserPromptSubmit")
    assert "updated by user" in context and "+- ship the importer" in context


def test_the_agents_own_edit_is_not_noticed(store, notes):
    store.append(notes, "Decisions", "use sqlite", by="agent")
    assert _run(PROMPT, store, notes) == ""
    store.append(notes, "Decisions", "and WAL", by="agent")
    assert _run(PROMPT, store, notes) == ""
    # Seen moved with them: a later user edit diffs against the agent's text.
    store.append(notes, "Pending", "tests", by="orchestrator")
    context = _context(_run(PROMPT, store, notes), "UserPromptSubmit")
    assert "updated by orchestrator" in context
    assert "+- tests" in context and "+- and WAL" not in context


def test_nothing_changed_since_the_last_prompt(store, notes):
    assert _run(PROMPT, store, notes) == ""  # the template, rev 0
    store.append(notes, "Decisions", "use sqlite", by="user")
    _run(PROMPT, store, notes)
    assert _run(PROMPT, store, notes) == ""


def test_a_text_newer_than_its_meta_is_not_noticed_twice(store, notes):
    """The engine writes the .md before the .json: a hook in between sees
    rev 2 with rev 3's text, and the next one has nothing new to say."""
    store.append(notes, "Decisions", "use sqlite", by="user")
    _run(PROMPT, store, notes)  # seen: rev 1
    store.append(notes, "Decisions", "and WAL", by="user")
    meta = store.path(notes).with_suffix(".json")
    rev2 = meta.read_text()
    store.append(notes, "Decisions", "no ORM", by="user")
    rev3 = meta.read_text()
    meta.write_text(rev2)
    context = _context(_run(PROMPT, store, notes), "UserPromptSubmit")
    assert "+- and WAL" in context and "+- no ORM" in context
    meta.write_text(rev3)
    assert _run(PROMPT, store, notes) == ""


def test_the_diff_is_capped(store, notes):
    _run(PROMPT, store, notes)
    text = "## Decisions\n" + "".join(f"- decision {i:04d}\n" for i in range(400))
    store.put(notes, text, if_rev=0, by="user")
    context = _context(_run(PROMPT, store, notes), "UserPromptSubmit")
    diff = context.partition("\n")[2]
    assert len(diff.encode()) <= DIFF_LIMIT + 100
    assert diff.endswith("… (diff clipped: read them with notes_read)")


def test_the_seen_file_is_private(store, notes):
    store.append(notes, "Decisions", "use sqlite", by="user")
    _run(PROMPT, store, notes)
    seen = store.path(notes).with_name(f"{notes}.seen.json")
    assert seen.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in store.root.iterdir() if p.name.startswith(".")] == []


HOOK_RUNS = 7
HOOK_ROUNDS = 5
# A cold interpreter running the hook, in seconds; or how many times a bare
# interpreter's start (python -c pass, timed alongside) it may take. Today
# ~22 ms, 1.5-2x a bare start, and 1.6x at load 30 on 20 cores; one heavy
# import (textual: ~170 ms, 14x) fails both.
HOOK_BUDGET = 0.15
HOOK_RATIO = 3.0


def test_it_starts_fast(store, notes, capsys):
    """Run on every prompt: a cold interpreter, no lazyagent imports.

    Robust under load (see tests/bench.py): the median of HOOK_RUNS per
    round, up to HOOK_ROUNDS rounds. A round passes under HOOK_BUDGET (times
    the CI factor), or under HOOK_RATIO times a bare interpreter timed in
    the same round: a loaded machine slows both alike."""
    store.append(notes, "Decisions", "use sqlite", by="user")

    def timed(fn) -> float:
        started = time.perf_counter()
        fn()
        return time.perf_counter() - started

    def bare() -> None:
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=10)

    scale = factor()
    rounds = []
    for _ in range(HOOK_ROUNDS):
        hook, base = [], []
        for _ in range(HOOK_RUNS):
            hook.append(timed(lambda: _run(PROMPT, store, notes)))
            base.append(timed(bare))
        rounds.append((statistics.median(hook), statistics.median(base)))
        took, start = rounds[-1]
        if took < HOOK_BUDGET * scale or took < HOOK_RATIO * start:
            break
    with capsys.disabled():
        print("\nBENCH notes hook, median of %d: %s (hook/bare ms), budget %.0f ms or %gx"
              % (HOOK_RUNS, ", ".join(f"{h * 1e3:.0f}/{b * 1e3:.0f}" for h, b in rounds),
                 HOOK_BUDGET * scale * 1e3, HOOK_RATIO))
    took, start = rounds[-1]
    assert took < HOOK_BUDGET * scale or took < HOOK_RATIO * start, rounds
    source = SCRIPT.read_text()
    assert "lazyagent" not in "".join(
        line for line in source.splitlines() if line.startswith(("import", "from"))
    )
