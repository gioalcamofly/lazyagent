"""A finished turn that ends with a question to the user (proposal 0013
I4h): the agent asked in prose ("Should I put it in this PR, or would you
prefer a separate task?") instead of a dialog or a brief, and waits.

:func:`turn_question` reads the turn's last assistant message and returns
the question, or None. It is conservative: only the end of the message
counts (its last paragraph, or a short list of options right after a
question), and a ``?`` in a code block, inline code, a table, a quote or a
heading never does.

:func:`last_assistant_text` reads that message from a Claude transcript
(JSONL) when the Stop hook did not carry it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# The longest question kept (the item's label).
QUESTION_CHARS = 300
# What may follow the question in its paragraph: one short sentence ("I
# haven't changed anything yet.").
TAIL_CHARS = 120
# A list of options after the question: at most this many lines.
MAX_OPTIONS = 6
# How much of a transcript's end is read.
TAIL_BYTES = 256 * 1024

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_LIST_LINE = re.compile(r"^\s*(?:[-*+•]|\d{1,2}[.)]|[A-Za-z][.)])\s+\S")
# Emphasis, quotes and brackets that may close a question: "**Which?**".
_CLOSERS = "*_\"'”’»)]"
# A sentence end: . ! ? (or …) before a space and the next sentence.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _paragraphs(text: str) -> list[str]:
    """The message's paragraphs, a fenced code block standing as one that is
    never a question."""
    out: list[str] = []
    current: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            if current:
                out.append("\n".join(current))
                current = []
            if fenced:
                out.append("\x00code")
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.strip():
            current.append(line.rstrip())
        elif current:
            out.append("\n".join(current))
            current = []
    if fenced:
        # An unclosed fence: the message ends inside code.
        out.append("\x00code")
    elif current:
        out.append("\n".join(current))
    return out


def _is_list(paragraph: str) -> bool:
    lines = paragraph.splitlines()
    return 0 < len(lines) <= MAX_OPTIONS and all(_LIST_LINE.match(x) for x in lines)


def _not_prose(paragraph: str) -> bool:
    """Code, a table, a quote or a heading: its ``?`` asks nothing."""
    if paragraph.startswith("\x00"):
        return True
    for line in paragraph.splitlines():
        s = line.lstrip()
        if s.startswith(("|", ">", "#")) or line.startswith(("    ", "\t")):
            return True
    return False


def _ask(paragraph: str) -> str | None:
    """The question that ends ``paragraph`` (one short sentence may follow
    it), or None."""
    if _not_prose(paragraph):
        return None
    flat = " ".join(paragraph.split())
    # A "?" in inline code is not the agent asking.
    flat = _INLINE_CODE.sub(lambda m: m.group(0).replace("?", "\x01"), flat)
    sentences = _SENTENCE_END.split(flat)
    for back in (1, 2):
        if len(sentences) < back:
            break
        sentence = sentences[-back].rstrip(_CLOSERS + " ")
        if not sentence.endswith("?"):
            continue
        tail = " ".join(sentences[len(sentences) - back + 1:])
        if back == 2 and (len(tail) > TAIL_CHARS or "?" in tail):
            break
        question = sentences[-back].replace("\x01", "?").strip()
        question = question.strip("*_ ")
        if len(question) < 2:
            return None
        if len(question) > QUESTION_CHARS:
            question = question[: QUESTION_CHARS - 1].rstrip() + "…"
        return question
    return None


def turn_question(text: str | None) -> str | None:
    """The question that ends an agent's last message to the user, or None.

    Counts: the last paragraph ends with a question ("…?" or "¿…?"), maybe
    followed by one short sentence; or the message ends with a short list of
    options right after a paragraph that does."""
    if not text or not text.strip():
        return None
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return None
    last = paragraphs[-1]
    if _is_list(last) and len(paragraphs) >= 2:
        return _ask(paragraphs[-2])
    return _ask(last)


def last_assistant_text(transcript: str | Path) -> str | None:
    """The text of the last assistant message in a Claude transcript (its
    text blocks, in order), or None when it cannot be read."""
    path = Path(transcript)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    message_id: str | None = None
    texts: list[str] = []
    for line in reversed(data.splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        kind = entry.get("type")
        if kind == "user" and message_id is not None:
            break
        if kind != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        mid = message.get("id")
        if message_id is not None and mid != message_id:
            break
        content = message.get("content")
        blocks = [content] if isinstance(content, str) else content or []
        found = [
            b if isinstance(b, str) else b.get("text", "")
            for b in blocks
            if isinstance(b, str) or (isinstance(b, dict) and b.get("type") == "text")
        ]
        if message_id is None:
            if not any(t.strip() for t in found):
                continue
            message_id = mid
        texts[:0] = found
        if mid is None:
            break
    text = "\n\n".join(t for t in texts if t.strip())
    return text or None
