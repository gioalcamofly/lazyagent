"""Review replies: an agent's answer to one review comment, in the diff view.

A client (the desktop app) sends review comments or a question to an agent
and registers their ids for that agent (``review_register``). The agent
answers each one with the ``review_reply`` MCP tool, which carries its token:
the engine takes a reply only from an agent a comment was registered for,
keeps it, and emits ``review.replied``. A client that was away reads the
kept ones with ``review_replies``.

The store is one JSON file per repo in the state dir
(``$XDG_STATE_HOME/lazyagent/reviews/<repo hash>.json``, 0600), rewritten on
each change (registrations and replies are rare and small); without a path
it lives in memory (throwaway engines, tests). Comments and replies older
than ``keep_days`` are dropped on load and on each change. Standard library
only.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lazyagent.engine.notes import _atomic_write
from lazyagent.engine.runtime import repo_digest, state_dir

log = logging.getLogger(__name__)

VERSION = 1
DEFAULT_KEEP_DAYS = 7
# A reply longer than this is refused: the thread is not a file transfer.
MAX_REPLY_BYTES = 16384
MAX_COMMENT_ID = 64


def reviews_path(repo_root: str) -> Path:
    return state_dir() / "reviews" / f"{repo_digest(repo_root)}.json"


@dataclass
class Registered:
    """A comment sent to agents: who may answer it."""

    worktree_path: str
    comment_id: str
    agents: list[str] = field(default_factory=list)
    registered_at: float = 0.0


@dataclass(frozen=True)
class Reply:
    reply_id: str
    comment_id: str
    worktree_path: str
    agent_id: str
    text: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReviewStore:
    """Registered comment ids per worktree, and the replies to them."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        keep_days: float = DEFAULT_KEEP_DAYS,
        clock: Callable[[], float] = time.time,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12],
    ) -> None:
        self._path = path
        self._keep = keep_days * 86400
        self._clock = clock
        self._new_id = new_id
        self._comments: dict[tuple[str, str], Registered] = {}
        self._replies: list[Reply] = []

    def load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            comments = [Registered(**c) for c in data.get("comments", [])]
            replies = [Reply(**r) for r in data.get("replies", [])]
        except (OSError, ValueError, TypeError) as e:
            log.warning("review replies at %s unreadable: %s", self._path, e)
            return
        self._comments = {(c.worktree_path, c.comment_id): c for c in comments}
        self._replies = replies
        if self._prune():
            self._save()

    def register(
        self, worktree_path: str, agent_id: str, comment_ids: Iterable[str]
    ) -> list[str]:
        """Lets ``agent_id`` in ``worktree_path`` answer ``comment_ids``
        (each comment keeps every agent it was sent to)."""
        now = self._clock()
        ids = []
        for comment_id in comment_ids:
            check_comment_id(comment_id)
            entry = self._comments.setdefault(
                (worktree_path, comment_id),
                Registered(worktree_path, comment_id),
            )
            if agent_id not in entry.agents:
                entry.agents.append(agent_id)
            entry.registered_at = now
            ids.append(comment_id)
        self._prune()
        self._save()
        return ids

    def reply(self, slot: tuple[str, str], comment_id: str, text: str) -> Reply:
        """Keeps the answer of the agent at ``slot`` (``(worktree_path,
        agent_id)``) to ``comment_id``; ValueError unless the comment was
        registered for that agent."""
        check_comment_id(comment_id)
        if not text.strip():
            raise ValueError("text is empty: write the answer to the comment")
        if len(text.encode()) > MAX_REPLY_BYTES:
            raise ValueError(
                f"text is over {MAX_REPLY_BYTES} bytes: answer briefly, "
                "the details can stay in the conversation"
            )
        worktree_path, agent_id = slot
        entry = self._comments.get((worktree_path, comment_id))
        if entry is None or agent_id not in entry.agents:
            raise ValueError(
                f"No review comment {comment_id!r} was sent to you: answer only "
                "the comment ids given in your prompt"
            )
        reply = Reply(
            reply_id=self._new_id(),
            comment_id=comment_id,
            worktree_path=worktree_path,
            agent_id=agent_id,
            text=text.strip(),
            at=self._clock(),
        )
        self._replies.append(reply)
        self._prune()
        self._save()
        return reply

    def replies(self, worktree_path: str | None = None) -> list[Reply]:
        """The kept replies (of one worktree), oldest first."""
        return [
            r
            for r in self._replies
            if worktree_path is None or r.worktree_path == worktree_path
        ]

    def _prune(self) -> bool:
        cutoff = self._clock() - self._keep
        before = (len(self._comments), len(self._replies))
        self._comments = {
            k: c for k, c in self._comments.items() if c.registered_at >= cutoff
        }
        self._replies = [r for r in self._replies if r.at >= cutoff]
        return before != (len(self._comments), len(self._replies))

    def _save(self) -> None:
        if self._path is None:
            return
        data = {
            "version": VERSION,
            "comments": [asdict(c) for c in self._comments.values()],
            "replies": [r.to_dict() for r in self._replies],
        }
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write(self._path, json.dumps(data, indent=1).encode())
        except OSError as e:
            log.warning("could not write review replies to %s: %s", self._path, e)


def check_comment_id(comment_id: Any) -> str:
    if (
        not isinstance(comment_id, str)
        or not comment_id
        or len(comment_id) > MAX_COMMENT_ID
        or not comment_id.isascii()
        or not all(ch.isalnum() or ch in "-_" for ch in comment_id)
    ):
        raise ValueError(
            "comment_id must be the id given in the prompt (letters, digits, - and _)"
        )
    return comment_id
