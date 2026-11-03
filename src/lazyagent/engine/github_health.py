"""Whether GitHub answers: one state the PR poller, the Actions poll and the
review requests search share.

There is no precheck: each poll just makes its usual ``gh`` call and tells
this state how it went. While calls fail (``gh`` not logged in, the network
down), the polls space their attempts out: 1, 2, 4, 8, then at most 10
minutes apart (:data:`BACKOFF_MAX`). The first call that answers ends the
outage and every poll is back to its normal cadence. What the polls fetched
last stays meanwhile: a failed round changes nothing.

A ``gh`` that is not installed is found once (:meth:`GithubHealth.set_installed`),
logged, and then no poll calls it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# The first retry comes after this long, then twice as long each time.
BACKOFF_BASE = 60.0
BACKOFF_MAX = 600.0
# A poll's timer drifts a little: an attempt this close to the retry time
# counts as due. Also what tells another poll's failure in the same round
# (it does not lengthen the backoff again).
SLACK = BACKOFF_BASE / 2
ERROR_CHARS = 200


def short(error: str) -> str:
    """``error`` on one line, at most :data:`ERROR_CHARS` long."""
    text = " ".join(str(error).split())
    return text if len(text) <= ERROR_CHARS else text[: ERROR_CHARS - 1] + "…"


def backoff(failures: int) -> float:
    """How long after the ``failures``-th failure in a row to try again."""
    return min(BACKOFF_BASE * 2 ** max(failures - 1, 0), BACKOFF_MAX)


class GithubHealth:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        # None until checked; False: no gh on PATH (found once, logged).
        self.installed_flag: bool | None = None
        self.failures = 0
        self.since: float | None = None
        self.error: str | None = None
        self.retry_at = 0.0
        # The last PR round that answered (the PR data's age).
        self.pr_data_at: float | None = None
        # Called on every change of what to_dict says.
        self.on_change: Callable[[], None] = lambda: None

    # --- gh installed ---

    def set_installed(self, value: bool) -> None:
        """What the one check found (the PR poller's first round)."""
        if value is False and self.installed_flag is not False:
            log.warning("gh is not installed: no PR, CI or review data from GitHub")
            self.installed_flag = False
            self.on_change()
            return
        self.installed_flag = value

    # --- Outages ---

    @property
    def available(self) -> bool:
        return self.installed_flag is not False and self.failures == 0

    def due(self, now: float | None = None) -> bool:
        """A poll may call GitHub now: no outage, or its next retry came."""
        if self.installed_flag is False:
            return False
        if self.failures == 0:
            return True
        now = self.clock() if now is None else now
        return now >= self.retry_at - SLACK

    def failed(self, error: str, started: float | None = None) -> None:
        """A call that ``started`` then failed. Another poll's failure in
        the same round does not count again."""
        started = self.clock() if started is None else started
        if self.failures and started < self.retry_at - SLACK:
            return
        self.failures += 1
        self.error = short(error) or "gh failed"
        self.retry_at = started + backoff(self.failures)
        if self.failures == 1:
            self.since = started
            log.warning("GitHub unavailable (gh: %s); keeping the last PR data, "
                        "retrying with backoff", self.error)
            self.on_change()

    def succeeded(self) -> None:
        if not self.failures:
            return
        down = self.clock() - (self.since or self.clock())
        log.info("GitHub available again after %.0f s (%d failed attempts)",
                 down, self.failures)
        self.failures = 0
        self.since = None
        self.error = None
        self.retry_at = 0.0
        self.on_change()

    def pr_round_ok(self, at: float) -> None:
        self.pr_data_at = at
        self.succeeded()

    def to_dict(self) -> dict[str, Any]:
        """``status_list.github`` and the ``github.updated`` event."""
        out: dict[str, Any] = {"available": self.available}
        if self.installed_flag is False:
            out["installed"] = False
            out["error"] = "gh is not installed"
        if self.failures:
            out.update(since=self.since, error=self.error, retry_at=self.retry_at)
        if self.pr_data_at is not None:
            out["pr_data_at"] = self.pr_data_at
        return out
