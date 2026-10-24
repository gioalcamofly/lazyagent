"""Work items from external feeders (proposal 0013, docs/design/inbox-plan.md
§2 "external", §5c "Projects" and "Waiting lane", I6).

A feeder (a planner, a script) tells the engine about work it tracks
elsewhere with ``work_items_upsert`` and ``work_items_close``. The engine
never invents tasks from them and never starts one: it stores what the
feeder said and shows it.

- **One record per URL** (dedupe). An upsert replaces the record's fields:
  an optional field left out goes back to its default (so an upsert
  without ``waiting`` clears it). A project's title and policy are the
  latest given for its id.
- **Caps.** ``[status] max_open_per_source`` (50) records per source and
  :data:`MAX_TOTAL` in all: a new URL past either is refused; updates of a
  known one are not. ``expires_at`` (Unix time) drops a record on its own.
- **Linking.** A record joins the work item row of the branch named in its
  ``branch``, else of the branch whose ticket key (``[status]
  ticket_pattern``, by default the generic ``ABC-123``) equals its
  ``ticket`` (ignoring case); with no ``ticket``, one is read from its URL
  with the same pattern. A record no local branch links to is "not
  started" (the counter), unless it is waiting.
- **Waiting.** ``waiting {reason?, until?}`` parks the linked row as the
  user's park gesture does (rule 7, zone ``parked``), as ``waiting
  (<source>)`` or ``waiting: <reason>``. It ends at ``until`` (a time or a
  park event) and stays over while the feeder sends the same value; the
  feeder's next upsert without it ends it too. The user's own park wins;
  the user's unpark (``status_gesture clear``) wins until the feeder sends
  a different ``waiting``.
- **Projects.** ``project {id, title, policy?}`` and ``depends_on [url]``
  are only stored and shown (rows group by project; "waits on <title>").
  With ``policy.continue_while_review`` the status projection wakes the
  orchestrator once when a task of the project becomes startable
  (status.py); the DAG and the next-task choice stay outside the engine.

Kept in ``$XDG_STATE_HOME/lazyagent/work_items/<repo hash>.json`` (0600,
atomic) by the engine that owns the repo, written at most once per
:data:`SAVE_EVERY`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent.engine.notes import _atomic_write
from lazyagent.engine.overlay import one_line
from lazyagent.engine.runtime import repo_digest, state_dir
from lazyagent.models import ticket_id

if TYPE_CHECKING:
    from lazyagent.engine.engine import Engine

log = logging.getLogger(__name__)

VERSION = 1
SAVE_EVERY = 1.0

PRIORITIES = ("high", "medium", "low")
DEFAULT_PRIORITY = "medium"
DEFAULT_MAX_OPEN_PER_SOURCE = 50
# Every source together.
MAX_TOTAL = 500
MAX_DEPENDS = 20

URL_CHARS = 500
TITLE_CHARS = 200
TICKET_CHARS = 64
BRANCH_CHARS = 200
PROJECT_ID_CHARS = 64
PROJECT_TITLE_CHARS = 80
REASON_CHARS = 60
SOURCE_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")

# The key prefix of a work item that has no branch yet.
EXT_PREFIX = "ext:"


def work_items_path(repo_root: str) -> Path:
    return state_dir() / "work_items" / f"{repo_digest(repo_root)}.json"


def waiting_text(source: str, reason: str | None) -> str:
    """The parked row's state: ``waiting: <reason>``, else ``waiting
    (<source>)``."""
    from lazyagent.engine.status import clip_words

    if reason:
        return clip_words(f"waiting: {reason}")
    return f"waiting ({source})"


def _url(value: Any, name: str = "url") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required: an http(s) URL")
    value = value.strip()
    if not value.startswith(("http://", "https://")) or any(c.isspace() for c in value):
        raise ValueError(f"{name} must be an http(s) URL with no spaces")
    if len(value) > URL_CHARS:
        raise ValueError(f"{name} is longer than {URL_CHARS} characters")
    return value


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return one_line(value, limit)


def _project(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("project must be {id, title, policy?}")
    project_id = _text(value.get("id"), "project.id", PROJECT_ID_CHARS, required=True)
    title = _text(value.get("title"), "project.title", PROJECT_TITLE_CHARS) or project_id
    out: dict[str, Any] = {"id": project_id, "title": title}
    policy = value.get("policy")
    if policy is not None:
        if not isinstance(policy, dict):
            raise ValueError("project.policy must be {continue_while_review?}")
        cwr = policy.get("continue_while_review", False)
        if type(cwr) is not bool:
            raise ValueError("project.policy.continue_while_review must be a boolean")
        out["policy"] = {"continue_while_review": cwr}
    return out


def _waiting(value: Any, now: float) -> dict[str, Any] | None:
    """``{reason?, until?}`` → the stored waiting (``raw`` is what the
    feeder sent, to tell a new value from the same one sent again)."""
    from lazyagent.engine.actionables import PARK_EVENTS, check_until

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("waiting must be {reason?, until?} or null")
    reason = _text(value.get("reason"), "waiting.reason", REASON_CHARS)
    raw_until = value.get("until")
    if raw_until is None:
        until: float | str = "indefinite"
    elif isinstance(raw_until, (int, float)) and not isinstance(raw_until, bool):
        # A time already past is accepted: the wait is simply over.
        until = float(raw_until)
    elif raw_until == "tomorrow" or raw_until in PARK_EVENTS:
        until = check_until(raw_until, now)
    else:
        raise ValueError(
            "waiting.until must be a Unix time, tomorrow, or one of " + ", ".join(PARK_EVENTS)
        )
    return {
        "reason": reason,
        "until": until,
        "raw": {"reason": reason, "until": raw_until},
        "at": now,
    }


class WorkItems:
    """The feeders' records; see the module docstring."""

    def __init__(
        self, engine: Engine | None = None, *, clock: Callable[[], float] | None = None
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._path: Path | None = None
        # url → record.
        self.records: dict[str, dict[str, Any]] = {}
        # project id → {id, title, policy}.
        self.projects: dict[str, dict[str, Any]] = {}
        self._saved: str | None = None
        self._handle: asyncio.TimerHandle | None = None

    @property
    def clock(self) -> Callable[[], float]:
        if self._clock is not None:
            return self._clock
        status = getattr(self._engine, "status", None)
        return status.clock if status is not None else time.time

    @property
    def _status_config(self) -> Any:
        config = getattr(self._engine, "config", None)
        return config.status if config is not None else None

    @property
    def max_open_per_source(self) -> int:
        config = self._status_config
        return config.max_open_per_source if config is not None else DEFAULT_MAX_OPEN_PER_SOURCE

    @property
    def ticket_pattern(self) -> str | None:
        config = self._status_config
        return config.ticket_pattern if config is not None else None

    # --- Store ---

    def open(self, path: Path | str) -> None:
        """Keep the records in ``path`` (load them now)."""
        self._path = Path(path)
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("could not read the work items from %s: %s", self._path, e)
            return
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return
        records = data.get("records")
        if isinstance(records, dict):
            self.records = {
                url: r for url, r in records.items()
                if isinstance(url, str) and isinstance(r, dict)
                and isinstance(r.get("title"), str) and isinstance(r.get("source"), str)
            }
        projects = data.get("projects")
        if isinstance(projects, dict):
            self.projects = {
                k: p for k, p in projects.items() if isinstance(k, str) and isinstance(p, dict)
            }
        self.expire(self.clock())
        self._saved = self._serialize()

    def _serialize(self) -> str:
        return json.dumps(
            {"version": VERSION, "records": self.records, "projects": self.projects},
            indent=1,
            sort_keys=True,
        )

    def changed(self) -> None:
        """Write within :data:`SAVE_EVERY` (now, with no event loop)."""
        if self._path is None or self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save()
            return
        self._handle = loop.call_later(SAVE_EVERY, self.save)

    def save(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if self._path is None:
            return
        text = self._serialize()
        if text == self._saved:
            return
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _atomic_write(self._path, text.encode())
            self._saved = text
        except OSError as e:
            log.warning("could not write the work items to %s: %s", self._path, e)

    # --- Writes ---

    def upsert(self, params: dict[str, Any], now: float) -> tuple[dict[str, Any], bool]:
        """Create or replace the record of ``params['url']``. Returns it and
        whether it is new. Raises ValueError on a bad field or a cap."""
        url = _url(params.get("url"))
        title = _text(params.get("title"), "title", TITLE_CHARS, required=True)
        source = params.get("source")
        if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
            raise ValueError(
                "source is required: 1-32 letters, digits, '_', '.' or '-' (e.g. 'planner')"
            )
        priority = params.get("priority")
        if priority is None:
            priority = DEFAULT_PRIORITY
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
        ticket = _text(params.get("ticket"), "ticket", TICKET_CHARS)
        branch = _text(params.get("branch"), "branch", BRANCH_CHARS)
        project = _project(params.get("project"))
        depends = params.get("depends_on")
        if depends is None:
            depends = []
        if not isinstance(depends, list):
            raise ValueError("depends_on must be a list of URLs")
        if len(depends) > MAX_DEPENDS:
            raise ValueError(f"depends_on has more than {MAX_DEPENDS} URLs")
        depends_on = list(dict.fromkeys(_url(d, "depends_on[]") for d in depends))
        if url in depends_on:
            raise ValueError("an item cannot depend on itself")
        expires_at = params.get("expires_at")
        if expires_at is not None:
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
                raise ValueError("expires_at must be a Unix time")
            if expires_at <= now:
                raise ValueError("expires_at is in the past")
            expires_at = float(expires_at)
        waiting = _waiting(params.get("waiting"), now)

        before = self.records.get(url)
        if before is None:
            cap = self.max_open_per_source
            mine = sum(r["source"] == source for r in self.records.values())
            if cap and mine >= cap:
                raise ValueError(
                    f"{source} already has {mine} open work items "
                    f"([status] max_open_per_source = {cap}): close some first"
                )
            if len(self.records) >= MAX_TOTAL:
                raise ValueError(f"{MAX_TOTAL} open work items in all: close some first")

        if waiting is not None and before is not None:
            old = before.get("waiting")
            if old is not None and old.get("raw") == waiting["raw"]:
                # The same value sent again: keep its start, its park and
                # whether it is over or the user unparked it.
                waiting = old
        record: dict[str, Any] = {
            "url": url,
            "title": title,
            "source": source,
            "priority": priority,
            "ticket": ticket,
            "branch": branch,
            "project": None,
            "depends_on": depends_on,
            "waiting": waiting,
            "expires_at": expires_at,
            "created_at": before["created_at"] if before is not None else now,
            "updated_at": now,
        }
        if project is not None:
            known = self.projects.get(project["id"], {})
            policy = project.get("policy", known.get("policy"))
            self.projects[project["id"]] = {
                "id": project["id"], "title": project["title"],
                **({"policy": policy} if policy is not None else {}),
            }
            record["project"] = project["id"]
        if before is not None and before.get("woke") is not None \
                and before.get("depends_on") == depends_on:
            record["woke"] = before["woke"]
        self.records[url] = record
        self._forget_projects()
        self.changed()
        return self.public(record), before is None

    def close(self, url: Any) -> dict[str, Any] | None:
        """Drop the record of ``url``; None when there was none."""
        url = _url(url)
        record = self.records.pop(url, None)
        if record is None:
            return None
        self._forget_projects()
        self.changed()
        return self.public(record)

    def expire(self, now: float) -> bool:
        """Drop the records past their ``expires_at``."""
        gone = [u for u, r in self.records.items()
                if isinstance(r.get("expires_at"), (int, float)) and r["expires_at"] <= now]
        for url in gone:
            del self.records[url]
        if gone:
            self._forget_projects()
            self.changed()
        return bool(gone)

    def _forget_projects(self) -> None:
        used = {r.get("project") for r in self.records.values()}
        for project_id in [p for p in self.projects if p not in used]:
            del self.projects[project_id]

    def override_waiting(self, url: str) -> bool:
        """The user unparked a waiting item: it stays unparked until the
        feeder sends a different ``waiting``."""
        record = self.records.get(url)
        waiting = record.get("waiting") if record is not None else None
        if not waiting or waiting.get("over") or waiting.get("overridden"):
            return False
        waiting["overridden"] = True
        self.changed()
        return True

    def set_woke(self, url: str, value: list[str] | None) -> None:
        record = self.records.get(url)
        if record is not None and record.get("woke") != value:
            record["woke"] = value
            self.changed()

    # --- Reads ---

    def ticket(self, record: dict[str, Any]) -> str | None:
        """The record's ticket, else the one its URL names."""
        if record.get("ticket"):
            return record["ticket"]
        return ticket_id(record["url"], self.ticket_pattern)

    def link(self, branch: str | None) -> dict[str, Any] | None:
        """The record ``branch`` links to: by its ``branch``, else by its
        ticket (the most recently updated one when several match)."""
        if not branch or not self.records:
            return None
        found = [r for r in self.records.values() if r.get("branch") == branch]
        if not found:
            key = ticket_id(branch, self.ticket_pattern)
            if key is None:
                return None
            folded = key.casefold()
            found = [r for r in self.records.values()
                     if (t := self.ticket(r)) is not None and t.casefold() == folded]
        return max(found, key=lambda r: r["updated_at"]) if found else None

    def unlinked(self, branches: Iterable[str]) -> list[dict[str, Any]]:
        """The records no branch in ``branches`` links to."""
        linked = set()
        for branch in branches:
            record = self.link(branch)
            if record is not None:
                linked.add(record["url"])
        return [r for r in self.records.values() if r["url"] not in linked]

    def project(self, project_id: str | None) -> dict[str, Any] | None:
        if not project_id:
            return None
        return self.projects.get(project_id) or {"id": project_id, "title": project_id}

    def continues_while_review(self, project_id: str | None) -> bool:
        project = self.project(project_id) or {}
        return bool((project.get("policy") or {}).get("continue_while_review"))

    @staticmethod
    def waiting_active(record: dict[str, Any]) -> bool:
        waiting = record.get("waiting")
        return bool(waiting) and not waiting.get("over") and not waiting.get("overridden")

    def waiting_gesture(self, record: dict[str, Any] | None, facts: Any, now: float
                        ) -> dict[str, Any] | None:
        """A waiting record's park, as a gesture on its row (rule 7), while it
        holds; one that ended is marked over."""
        from lazyagent.engine.actionables import new_park, park_holds
        from lazyagent.engine.overlay import _place

        if record is None or not self.waiting_active(record):
            return None
        waiting = record["waiting"]
        by = f"feeder:{record['source']}"
        place = _place(facts)
        if not isinstance(waiting.get("park"), dict):
            waiting["park"] = new_park(waiting["until"], place, now, by)
            self.changed()
        before = json.dumps(waiting["park"], sort_keys=True)
        holds = park_holds(waiting["park"], place, now)
        if not holds:
            waiting["over"] = True
        if not holds or json.dumps(waiting["park"], sort_keys=True) != before:
            self.changed()
        if not holds:
            return None
        gesture: dict[str, Any] = {
            "kind": "park",
            "at": waiting["at"],
            "by": by,
            "until": waiting["until"],
            "waiting": True,
            "source": record["source"],
        }
        if waiting.get("reason"):
            gesture["text"] = waiting["reason"]
        return gesture

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        """A record as the methods and rows show it."""
        waiting = record.get("waiting")
        return {
            "url": record["url"],
            "title": record["title"],
            "source": record["source"],
            "priority": record["priority"],
            "ticket": self.ticket(record),
            "branch": record.get("branch"),
            "project": self.project(record.get("project")),
            "depends_on": list(record.get("depends_on") or []),
            "waiting": None if not waiting else {
                "reason": waiting.get("reason"),
                "until": waiting.get("until"),
                "since": waiting.get("at"),
                "active": self.waiting_active(record),
            },
            "expires_at": record.get("expires_at"),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }
