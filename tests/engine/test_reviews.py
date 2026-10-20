"""The review replies store: who may answer which comment, what is kept."""

from __future__ import annotations

import json

import pytest

from lazyagent.engine.reviews import MAX_REPLY_BYTES, ReviewStore

FEAT = "/r/feat"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_only_an_agent_a_comment_was_sent_to_may_answer_it():
    store = ReviewStore()
    store.register(FEAT, "a1", ["c1", "c2"])

    reply = store.reply((FEAT, "a1"), "c1", "  Renamed it.\n")
    assert (reply.comment_id, reply.agent_id, reply.text) == ("c1", "a1", "Renamed it.")

    with pytest.raises(ValueError, match="No review comment .* was sent to you"):
        store.reply((FEAT, "a2"), "c1", "me too")
    with pytest.raises(ValueError, match="No review comment .* was sent to you"):
        store.reply(("/r/other", "a1"), "c1", "elsewhere")
    with pytest.raises(ValueError, match="No review comment .* was sent to you"):
        store.reply((FEAT, "a1"), "c9", "never sent")
    assert [r.text for r in store.replies(FEAT)] == ["Renamed it."]


def test_a_comment_sent_again_keeps_every_agent_it_went_to():
    store = ReviewStore()
    store.register(FEAT, "a1", ["c1"])
    store.register(FEAT, "a2", ["c1"])
    store.reply((FEAT, "a1"), "c1", "first")
    store.reply((FEAT, "a2"), "c1", "second")
    got = [(r.agent_id, r.text) for r in store.replies()]
    assert got == [("a1", "first"), ("a2", "second")]


@pytest.mark.parametrize("comment_id", ["", "a b", "x" * 65, "é1", "c1\n", 7, None])
def test_comment_ids_are_short_plain_words(comment_id):
    store = ReviewStore()
    with pytest.raises(ValueError, match="comment_id"):
        store.register(FEAT, "a1", [comment_id])


def test_an_empty_or_huge_reply_is_refused():
    store = ReviewStore()
    store.register(FEAT, "a1", ["c1"])
    with pytest.raises(ValueError, match="empty"):
        store.reply((FEAT, "a1"), "c1", " \n")
    with pytest.raises(ValueError, match="bytes"):
        store.reply((FEAT, "a1"), "c1", "x" * (MAX_REPLY_BYTES + 1))


def test_replies_survive_a_restart_and_expire(tmp_path):
    path = tmp_path / "reviews" / "repo.json"
    clock = Clock()
    store = ReviewStore(path, keep_days=7, clock=clock)
    store.register(FEAT, "a1", ["c1"])
    store.reply((FEAT, "a1"), "c1", "kept")
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    again = ReviewStore(path, keep_days=7, clock=clock)
    again.load()
    assert [r.text for r in again.replies(FEAT)] == ["kept"]
    again.reply((FEAT, "a1"), "c1", "a restored agent answers too")

    clock.now += 8 * 86400
    later = ReviewStore(path, keep_days=7, clock=clock)
    later.load()
    assert later.replies() == []
    assert json.loads(path.read_text())["comments"] == []
    with pytest.raises(ValueError, match="No review comment .* was sent to you"):
        later.reply((FEAT, "a1"), "c1", "too late")


def test_an_unreadable_file_starts_empty(tmp_path):
    path = tmp_path / "reviews.json"
    path.write_text("{not json")
    store = ReviewStore(path)
    store.load()
    assert store.replies() == []
