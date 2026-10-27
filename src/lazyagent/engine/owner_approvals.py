"""The owner's approval of a PR head, recorded outside GitHub
(docs/design/rules-plan.md R3d).

In the "owner review → human peer review → QA → merge" flow the user
reviews a PR outside GitHub and says so to the agent ("approved"). The PR
leaving draft was the only trace of that (``pr.commits_after_ready``); a
PR already ready that gets a real commit had no way back to "approved"
short of a draft round trip, and a PR opened ready counted every commit
since its opening for good.

- **Record**, per work item in the overlay (``owner_approvals``): ``{key,
  branch, pr, sha, at, source, note, agent_id?, previous?}``, ``source``
  one of ``home`` (the Approve action), ``agent`` (the ``owner_approve``
  MCP tool, right after the user said so in the agent's conversation),
  ``cli`` (``lazyagent pr approve``). Always the PR's current head: the
  engine reads the PR again first, and refuses a ``sha`` that is not that
  head (Home shows a head that has moved since). Never written to GitHub.
- **Revoke** (Why?, ``lazyagent pr approve --revoke``) drops it; the undo
  of an approval brings back the one it replaced.
- **Counting**, off the loop as ``merged_into`` is, once per (approved
  commit, head, base): :meth:`BaseMerges.commits_since`, the real commits
  on the head after the approved one, clean merges of the base left out as
  R3c does. An approved commit no longer in the head's history (a force
  push) is ``lost``: the fact counts from the ready event again.
- **The fact** (``pr.commits_after_owner_approval``, rules/facts.py):
  counts from the newer of the last ready event and the approval.

Its timeline entries (``owner_approval``) read "you approved <sha7>".
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lazyagent.engine.overlay import one_line

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine
    from lazyagent.models import PrInfo

log = logging.getLogger(__name__)

SOURCES = ("home", "agent", "cli")
NOTE_CHARS = 200
# Counts kept (key, approved sha, head, base) → result.
_COUNTS_MAX = 512


@dataclass(frozen=True)
class ApprovalFact:
    """A standing approval as a row reads it."""

    sha: str
    at: float
    source: str
    # ``head``: the PR's head is the approved commit; ``counted``: ``after``
    # real commits since (and ``merges`` clean base merges, not counted);
    # ``lost``: the approved commit is not in the head's history; ``pending``:
    # not counted yet.
    state: str
    after: int | None = None
    merges: int | None = None
    # Of ``merges``, those where only generated files differ (R4c).
    generated: int = 0


def ready_time(pr: PrInfo) -> float | None:
    """The PR's ready point (``ready_at``) as a Unix time."""
    if not pr.ready_at:
        return None
    try:
        return dt.datetime.fromisoformat(pr.ready_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def after_owner_approval(pr: PrInfo, approval: ApprovalFact | None) -> int | None:
    """``pr.commits_after_owner_approval``: the real commits after the newer
    of the last ready event and the approval. A draft is 0, as for
    ``commits_after_ready``. An approval that cannot be counted (not yet, or
    its commit is gone) leaves the ready count; None: unknown."""
    ready = pr.commits_after_ready
    if pr.is_draft:
        return 0
    if approval is None:
        return ready
    since = ready_time(pr)
    if ready is not None and since is not None and since > approval.at:
        return ready
    if approval.state == "head":
        return 0
    if approval.state == "counted":
        return approval.after
    if approval.state == "pending":
        return None
    return ready


def why_text(pr: PrInfo | None, approval: ApprovalFact | None) -> str:
    """One line for Why?: what the approval is and where the count starts."""
    if approval is None:
        return ""
    sha7 = approval.sha[:7]
    head = f"you approved {sha7} ({approval.source})"
    if pr is not None and pr.is_draft:
        return f"{head}; the PR is a draft: no commits counted"
    since = ready_time(pr) if pr is not None else None
    if pr is not None and pr.commits_after_ready is not None and since is not None \
            and since > approval.at:
        return f"{head}; the PR was marked ready after it: counting from that"
    if approval.state == "head":
        return f"{head}: the PR's head, no commits since"
    if approval.state == "counted":
        gen = (f"; {approval.generated} of them: base merge, only generated files differ"
               if approval.generated else "")
        merges = (f" ({approval.merges} clean merge{'s' if approval.merges != 1 else ''} "
                  f"of the base not counted{gen})" if approval.merges else "")
        n = approval.after
        return f"{head}: {n} commit{'s' if n != 1 else ''} since{merges}"
    if approval.state == "pending":
        return f"{head}: counting the commits since"
    if pr is not None and pr.commits_after_ready is not None:
        return (f"{head}, but {sha7} is no longer in the PR's history (a force push?): "
                "counting from the ready event")
    return (f"{head}, but {sha7} is no longer in the PR's history (a force push?) and "
            "there is no ready event: unknown")


def public(record: dict[str, Any], fact: ApprovalFact | None = None,
           pr: PrInfo | None = None) -> dict[str, Any]:
    """An approval as clients see it (rules_explain, owner_approve's answer)."""
    out = {k: record.get(k) for k in ("key", "branch", "pr", "sha", "at", "source", "note",
                                      "agent_id")}
    out["has_previous"] = isinstance(record.get("previous"), dict)
    if fact is not None:
        out.update(state=fact.state, after=fact.after, merges=fact.merges)
        out["text"] = why_text(pr, fact)
    return out


def _sha_matches(given: str, sha: str) -> bool:
    given = given.strip().lower()
    return len(given) >= 7 and sha.lower().startswith(given)


class OwnerApprovals:
    """The engine's owner approvals (the overlay's table)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        # (key, approved sha, head, base) → (commits, clean merges, those
        # where only generated files differ), or None when the approved
        # commit is not in the head's history.
        self._counts: dict[tuple[str, str, str, str], tuple[int, int, int] | None] = {}

    @property
    def store(self) -> dict[str, dict[str, Any]]:
        return self._engine.overlay.owner_approvals

    def _changed(self) -> None:
        self._engine.overlay.changed()
        self._engine.status.mark_dirty()

    # --- What a pass reads ---

    def fact(self, key: str, pr: PrInfo | None) -> ApprovalFact | None:
        """``key``'s approval for this PR (one recorded for another PR on the
        branch is not this one's)."""
        r = self.store.get(key)
        if r is None or pr is None or (r.get("pr") and r["pr"] != pr.number):
            return None
        sha, at, source = r["sha"], float(r["at"]), r.get("source", "")
        if pr.head_sha and sha == pr.head_sha:
            return ApprovalFact(sha, at, source, "head")
        slot = (key, sha, pr.head_sha, pr.base_ref)
        if slot not in self._counts:
            return ApprovalFact(sha, at, source, "pending")
        counted = self._counts[slot]
        if counted is None:
            return ApprovalFact(sha, at, source, "lost")
        return ApprovalFact(sha, at, source, "counted", counted[0], counted[1],
                            counted[2] if len(counted) > 2 else 0)

    def of_key(self, key: str, pr: PrInfo | None = None) -> dict[str, Any] | None:
        r = self.store.get(key)
        return public(r, self.fact(key, pr), pr) if r is not None else None

    async def refresh(self) -> None:
        """Count the commits after each standing approval whose head moved:
        git, once per (approved commit, head, base), off the loop."""
        engine = self._engine
        repo_root = engine.repo_root
        if not repo_root or not self.store:
            return
        todo = []
        for key, r in list(self.store.items()):
            path = engine.status.path_of(key)
            pr = engine.get_pr_info(path) if path is not None else None
            if pr is None or pr.state != "OPEN" or not pr.head_sha or pr.head_sha == r["sha"]:
                continue
            slot = (key, r["sha"], pr.head_sha, pr.base_ref)
            if slot not in self._counts:
                todo.append(slot)
        if not todo:
            return
        merges = engine.worktree_service.base_merges
        found = await asyncio.to_thread(
            lambda: {s: merges.commits_since(repo_root, s[1], s[2], s[3]) for s in todo})
        if len(self._counts) + len(found) > _COUNTS_MAX:
            self._counts.clear()
        self._counts.update(found)
        engine.status.mark_dirty()

    # --- Recording ---

    async def approve(self, key: str, *, source: str, sha: Any = None, note: Any = "",
                      agent_id: str | None = None) -> dict[str, Any]:
        """Record the user's approval of ``key``'s PR at its head, read
        again first. ``sha``: the head the user saw (refused when the PR's
        head is another)."""
        engine = self._engine
        if source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ValueError("note must be a string")
        if sha is not None and (not isinstance(sha, str) or len(sha.strip()) < 7):
            raise ValueError("sha must be the head you reviewed (at least 7 characters)")
        path = engine.status.path_of(key)
        wt = engine.worktree(path) if path is not None else None
        if wt is None:
            raise ValueError(f"No work item {key!r} with a worktree")
        pr = await engine.worktree_service.ensure_pr_info(path, refresh=True)
        if pr is None or pr.state != "OPEN" or not pr.head_sha:
            raise ValueError(f"{key} has no open PR to approve")
        head = pr.head_sha
        if sha is not None and not _sha_matches(sha, head):
            raise ValueError(f"The PR's head is {head[:7]} now, not {sha.strip()[:7]}: it moved "
                             "since you looked. Review it again, then approve.")
        now = engine.status.clock()
        old = self.store.get(key)
        record: dict[str, Any] = {"key": key, "branch": wt.branch, "pr": pr.number, "sha": head,
                                  "at": now, "source": source,
                                  "note": one_line(note, NOTE_CHARS) if note.strip() else ""}
        if agent_id:
            record["agent_id"] = agent_id
        if old is not None:
            record["previous"] = {k: v for k, v in old.items() if k != "previous"}
        self.store[key] = record
        how = {"home": "", "cli": " (from the CLI)",
               "agent": f" (recorded by agent {agent_id})" if agent_id else " (by its agent)"}
        engine.overlay.add(key, "owner_approval", "user" if source != "agent" else
                           f"agent:{agent_id}", f"you approved {head[:7]}{how[source]}",
                           ref=head, at=now)
        self._changed()
        out = public(record, self.fact(key, pr), pr)
        local = getattr(wt, "head", None)
        if source == "agent" and local and local != head:
            out["warning"] = (f"The PR's head is {head[:7]} but your worktree is at "
                              f"{local[:7]}: the approval is of what is pushed.")
        return out

    def revoke(self, key: str, *, sha: Any = None, undo: bool = False) -> dict[str, Any]:
        """Take ``key``'s approval back. ``undo``: the approval it replaced,
        if any, stands again. ``sha``: only when it is still that one."""
        engine = self._engine
        r = self.store.get(key)
        if r is None:
            raise ValueError(f"{key} has no approval of yours to revoke")
        if sha is not None and (not isinstance(sha, str) or not _sha_matches(sha, r["sha"])):
            raise ValueError(f"{key}'s approval is of {r['sha'][:7]}, not {sha}")
        previous = r.get("previous")
        if undo and isinstance(previous, dict) and isinstance(previous.get("sha"), str):
            self.store[key] = previous
            text = f"undone: your approval of {r['sha'][:7]} ({previous['sha'][:7]} stands)"
        else:
            del self.store[key]
            text = f"you took back your approval of {r['sha'][:7]}"
        engine.overlay.add(key, "owner_approval", "user", text, ref=r["sha"])
        self._changed()
        out = public(r)
        out["revoked"] = True
        standing = self.store.get(key)
        out["standing"] = public(standing) if standing is not None else None
        return out
