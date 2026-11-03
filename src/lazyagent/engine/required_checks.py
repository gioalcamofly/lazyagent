"""The checks that gate a merge, read from GitHub (proposal 0014 R5a).

Which checks a PR must pass is GitHub's to say: the base branch's
protection and the rulesets that apply to it. Per base, two reads a read-only
user may make:

- ``repos/{owner}/{repo}/branches/<base>``: ``protection.required_status_checks``
  (``contexts``, and ``checks[].context``). Unlike ``.../protection``, it
  needs no admin rights.
- ``repos/{owner}/{repo}/rules/branches/<base>``: the active rules, of
  which ``required_status_checks`` (``parameters.required_status_checks[].context``).
  When GitHub refuses it (403, 404), the protection alone counts.

**A base that requires nothing** (a project branch, typically) borrows the
repo's default branch's set (``repos/{owner}/{repo}``'s ``default_branch``),
because that is where the code lands: source ``default_branch``.

**Cache.** Per branch, :data:`TTL` (6 h); a stale set stays in use until a
new read answers. A base read only while a PR on it is red (the set only
matters then), at most :data:`MAX_FETCHES` bases a poll, every call through
the Actions hub (its caps, its back-off and the shared GitHub state). A read
GitHub refuses (403, 404) is cached as unreadable for the same time; a call
that got no answer at all (an outage) is not cached.

**Unreadable** (no read answered yet, or GitHub refused it): no set, and
the gating is exactly as before, every red gates unless ``[ci.check]`` says
otherwise. ``[ci] gating_from_protection = false`` turns all of this off.

The names are the rollup's: a check run's name (the job's, matrix values
included) or a commit status's context. :meth:`Required.covers` matches a
check by its job name, ``<workflow> / <job>``, its check id or a status
context, without case.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from lazyagent.engine.trackers import TrackerError

if TYPE_CHECKING:
    from lazyagent.engine.actions import ActionsHub
    from lazyagent.models import PrInfo

log = logging.getLogger(__name__)

TTL = 6 * 3600.0
# Bases read per poll (each: two calls; the default branch's, three).
MAX_FETCHES = 2
BRANCH_PATH = "repos/{owner}/{repo}/branches/"
RULES_PATH = "repos/{owner}/{repo}/rules/branches/"
REPO_PATH = "repos/{owner}/{repo}"
# A required check that ended so counts as passed (GitHub's own rule).
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")
SOURCES = ("protection", "default_branch", "config", "none")


@dataclass(frozen=True)
class Required:
    """A base's required checks, as the rollup names them."""

    names: tuple[str, ...]
    # protection · default_branch
    source: str
    base: str
    # The branch the set was read from (the base, or the default branch).
    read_from: str

    def covers(self, *names: str) -> bool:
        """Any of ``names`` (a job name, ``<workflow> / <job>``, a check id,
        a status context) is required."""
        wanted = {n.lower() for n in self.names}
        return any(n and n.lower() in wanted for n in names)

    def all_passed(self, pr: PrInfo | None) -> bool:
        """Every required check has reported on the PR's head, and passed."""
        if pr is None or not self.names:
            return False
        done = {c.name.lower() for c in pr.checks
                if c.status.upper() in ("COMPLETED", "") and c.conclusion.upper() in PASSED}
        return all(n.lower() in done for n in self.names)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "base": self.base, "read_from": self.read_from,
                "checks": list(self.names)}


def parse_branch(body: str) -> tuple[str, ...]:
    """``branches/<b>``'s required status checks (none when unprotected);
    raises ValueError on a bad body."""
    try:
        data = json.loads(body)
        rsc = ((data.get("protection") or {}).get("required_status_checks") or {})
        names = [str(x) for x in rsc.get("contexts") or [] if x]
        names += [str(c.get("context")) for c in rsc.get("checks") or []
                  if isinstance(c, dict) and c.get("context")]
    except (AttributeError, TypeError, ValueError) as e:
        raise ValueError(f"bad branch response: {e}") from e
    return _unique(names)


def parse_rules(body: str) -> tuple[str, ...]:
    """``rules/branches/<b>``'s ``required_status_checks`` contexts."""
    try:
        rules = json.loads(body)
        if not isinstance(rules, list):
            raise TypeError("not a list")
        names = [str(c.get("context"))
                 for r in rules if isinstance(r, dict) and r.get("type") == "required_status_checks"
                 for c in ((r.get("parameters") or {}).get("required_status_checks") or [])
                 if isinstance(c, dict) and c.get("context")]
    except (AttributeError, TypeError, ValueError) as e:
        raise ValueError(f"bad rules response: {e}") from e
    return _unique(names)


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for n in names:
        seen.setdefault(n.lower(), n)
    return tuple(sorted(seen.values(), key=str.lower))


@dataclass
class _Entry:
    # None: unreadable (GitHub refused the read).
    names: tuple[str, ...] | None
    at: float


class RequiredChecks:
    """Per repo, beside the Actions hub that makes its calls."""

    def __init__(self, hub: ActionsHub) -> None:
        self.hub = hub
        self.branches: dict[str, _Entry] = {}
        # The repo's default branch (None: unreadable) and when it was read.
        self.default: _Entry | None = None
        self.version = 0

    def clear(self) -> bool:
        """Forget every set (the switch is off); True when there was one."""
        if not self.branches and self.default is None:
            return False
        self.branches.clear()
        self.default = None
        self.version += 1
        return True

    def _fresh(self, entry: _Entry | None, now: float) -> bool:
        return entry is not None and now - entry.at < TTL

    @property
    def default_branch(self) -> str | None:
        d = self.default
        return d.names[0] if d is not None and d.names else None

    def for_base(self, base: str | None) -> Required | None:
        """The base's set from the cache: its own, else the default
        branch's; None when nothing is known or nothing is required."""
        if not base:
            return None
        own = self.branches.get(base)
        if own is None or own.names is None:
            return None
        if own.names:
            return Required(own.names, "protection", base, base)
        default = self.default_branch
        if default and default != base:
            theirs = self.branches.get(default)
            if theirs is not None and theirs.names:
                return Required(theirs.names, "default_branch", base, default)
        return None

    def known(self) -> bool:
        """Some base has a set: the gating reads it."""
        return any(e.names for e in self.branches.values())

    async def fetch(self, bases: Iterable[str]) -> bool:
        """Read what the bases lack (stale or never read), within
        :data:`MAX_FETCHES`; True when a set changed."""
        now = self.hub.clock()
        changed = False
        reads = 0
        try:
            for base in sorted({b for b in bases if b}):
                if reads >= MAX_FETCHES:
                    break
                read = False
                if not self._fresh(self.branches.get(base), now):
                    changed |= await self._read(base, now)
                    read = True
                own = self.branches.get(base)
                if own is not None and own.names == ():
                    # Readable, and it requires nothing: the default
                    # branch's set, where the code lands.
                    if not self._fresh(self.default, now):
                        changed |= await self._read_default(now)
                        read = True
                    default = self.default_branch
                    if default and default != base \
                            and not self._fresh(self.branches.get(default), now):
                        changed |= await self._read(default, now)
                        read = True
                reads += read
        except _Outage:
            pass
        if changed:
            self.version += 1
        return changed

    async def _get(self, path: str) -> str | None:
        """A body, or None when GitHub refused (4xx); raises _Outage when
        no answer came (or the hub may not call now)."""
        from lazyagent.engine.actions import HttpError

        try:
            _, _, body = await self.hub._api(path)
        except HttpError:
            return None
        except (TrackerError, OSError) as e:
            raise _Outage(str(e)) from e
        return body

    async def _read_default(self, now: float) -> bool:
        body = await self._get(REPO_PATH)
        name = None
        if body is not None:
            try:
                name = str(json.loads(body).get("default_branch") or "") or None
            except (AttributeError, TypeError, ValueError):
                name = None
        before = self.default_branch
        self.default = _Entry((name,) if name else None, now)
        return name != before

    async def _read(self, branch: str, now: float) -> bool:
        ref = quote(branch, safe="/")
        body = await self._get(BRANCH_PATH + ref)
        names: tuple[str, ...] | None = None
        if body is not None:
            try:
                names = parse_branch(body)
            except ValueError as e:
                log.warning("required checks of %s: %s", branch, e)
        if names is not None:
            # No answer here either: nothing cached, both are read again.
            rules = await self._get(RULES_PATH + ref)
            if rules is not None:
                try:
                    names = _unique(names + parse_rules(rules))
                except ValueError as e:
                    log.debug("rulesets of %s: %s", branch, e)
        old = self.branches.get(branch)
        self.branches[branch] = _Entry(names, now)
        if old is None or old.names != names:
            log.info("required checks of %s: %s", branch,
                     "unreadable" if names is None else (", ".join(names) or "none"))
            return True
        return False


class _Outage(Exception):
    """No answer from GitHub (or no call allowed now): try again later."""
