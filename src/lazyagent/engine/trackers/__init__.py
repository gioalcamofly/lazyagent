"""Optional issue-tracker sources (proposal 0013, inbox-plan §7b).

An adapter reads one tracker, read-only: :meth:`TrackerAdapter.fetch` the
tasks linked to work items (by a key read from the branch name) and, if it
can, :meth:`TrackerAdapter.assigned_to_me`. The engine never writes to a
tracker.

:class:`TrackerHub` polls every enabled adapter for the status projection:
only the keys of live rows (plus the assigned tasks, if on), batched, every
``poll_s`` at rest and at once for a new key, under a per-adapter cap on
calls per minute, backing off on 429; on an error the last good value is
kept (and marked stale). It maps each task's raw status to ``{ball, state,
level}`` and opens an inbox item when a task *enters* a status mapped with a
``level`` (dedupe ``tracker:<adapter>:<key>:<status>``), resolving it when
the task leaves; the inbox queue shows them (``tracker_status``).

Config: ``$XDG_CONFIG_HOME/lazyagent/trackers.toml``, one table per adapter;
a repo's ``.lazyagent/trackers.toml`` may change only the ``[<adapter>.status]``
mappings. Credentials are named there by reference (``token_env``,
``keyring``), never stored by lazyagent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lazyagent.models import TICKET_PATTERN, ticket_id

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

TRACKERS_FILENAME = "trackers.toml"
REPO_DIR = ".lazyagent"
ENTRY_POINT_GROUP = "lazyagent.trackers"

DEFAULT_POLL_S = 300
DEFAULT_MAX_CALLS_PER_MIN = 30
# Every adapter call is cut off after this long.
CALL_TIMEOUT = 30.0
# Back-off after a 429 with no Retry-After: doubles up to the cap.
BACKOFF_MIN = 60.0
BACKOFF_MAX = 3600.0

BALLS = ("you", "agent", "others", "stalled", "done")
PRIORITIES = ("high", "medium", "low")
LEVELS = ("now", "round")

# models.ticket_id's pattern: the default key in a branch name.
GENERIC_KEY = TICKET_PATTERN


@dataclass(frozen=True)
class TrackerTask:
    key: str
    url: str
    title: str
    status_raw: str
    assignee: str | None = None
    updated_at: float = 0.0
    etag: str | None = None
    # high, medium or low, when the adapter can tell (optional).
    priority: str | None = None


@dataclass(frozen=True)
class StatusRule:
    """Where a tracker status puts a work item. ``ball`` None: unchanged."""

    ball: str | None = None
    state: str | None = None
    level: str | None = None


@dataclass
class TrackerConfig:
    name: str
    enabled: bool = False
    poll_s: int = DEFAULT_POLL_S
    max_calls_per_min: int = DEFAULT_MAX_CALLS_PER_MIN
    assigned_to_me: bool = True
    key_pattern: str | None = None
    status: dict[str, StatusRule] = field(default_factory=dict)
    # The adapter's own keys (status_from, base_url, token_env, ...).
    options: dict[str, Any] = field(default_factory=dict)

    def rule(self, status_raw: str) -> StatusRule | None:
        """The mapping for a raw status: exact, else ignoring case."""
        if status_raw in self.status:
            return self.status[status_raw]
        folded = status_raw.casefold()
        for name, rule in self.status.items():
            if name.casefold() == folded:
                return rule
        return None


class TrackerError(Exception):
    """A tracker call failed; the last good value stays."""


class RateLimited(TrackerError):
    """The tracker said 429 (or its rate-limit equivalent)."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TrackerAdapter(Protocol):
    name: str

    def key_pattern(self) -> str | None:
        """A regex for the key in a branch name (its first group, else the
        whole match); None: :data:`GENERIC_KEY`."""
        ...

    async def fetch(self, keys: list[str]) -> list[TrackerTask]:
        """The tasks for ``keys``; a key with no task is left out."""
        ...

    async def assigned_to_me(self) -> list[TrackerTask]:
        """Open tasks assigned to the user; may raise NotImplementedError."""
        ...


# (config, repo root) → adapter.
AdapterFactory = Callable[[TrackerConfig, str], TrackerAdapter]


# ----------------------------------------------------------------------
# Registry and config
# ----------------------------------------------------------------------


def builtin_adapters() -> dict[str, AdapterFactory]:
    from lazyagent.engine.trackers.github_issues import GithubIssues
    from lazyagent.engine.trackers.jira import Jira

    return {GithubIssues.name: GithubIssues, Jira.name: Jira}


def adapter_factories() -> dict[str, AdapterFactory]:
    """The built-in adapters plus those of installed packages (entry point
    group ``lazyagent.trackers``); a package cannot replace a built-in."""
    factories: dict[str, AdapterFactory] = {}
    try:
        from importlib.metadata import entry_points

        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # noqa: BLE001 - a broken environment loses plugins only
        found = []
    for ep in found:
        try:
            factories[ep.name] = ep.load()
        except Exception:  # noqa: BLE001
            log.warning("tracker adapter %s failed to load", ep.name, exc_info=True)
    factories.update(builtin_adapters())
    return factories


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/lazyagent`` (``~/.config/lazyagent`` by default)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lazyagent"


def _read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None or not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        log.warning("ignoring %s: %s", path, e)
        return {}


def _status_rules(data: object) -> dict[str, StatusRule]:
    """``[<adapter>.status]``: a bad entry is skipped. A ``level`` makes the
    ball ``you``; ball ``you`` with no level is ``round``."""
    rules: dict[str, StatusRule] = {}
    if not isinstance(data, dict):
        return rules
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        ball = value.get("ball")
        state = value.get("state", value.get("state_text"))
        level = value.get("level")
        if ball is not None and ball not in BALLS:
            continue
        if level is not None and level not in LEVELS:
            continue
        if level is not None:
            ball = "you"
        elif ball == "you":
            level = "round"
        rules[str(name)] = StatusRule(
            ball, state.strip() if isinstance(state, str) and state.strip() else None, level
        )
    return rules


_SHARED = {"enabled", "poll_s", "max_calls_per_min", "assigned_to_me", "key_pattern", "status"}


def _tracker_config(name: str, data: dict[str, Any]) -> TrackerConfig:
    config = TrackerConfig(name=name, enabled=data.get("enabled") is True)
    for attr in ("poll_s", "max_calls_per_min"):
        value = data.get(attr)
        if type(value) is int and value > 0:
            setattr(config, attr, value)
    if isinstance(data.get("assigned_to_me"), bool):
        config.assigned_to_me = data["assigned_to_me"]
    pattern = data.get("key_pattern")
    if isinstance(pattern, str) and pattern:
        try:
            re.compile(pattern)
            config.key_pattern = pattern
        except re.error:
            log.warning("tracker %s: bad key_pattern %r", name, pattern)
    config.status = _status_rules(data.get("status"))
    config.options = {k: v for k, v in data.items() if k not in _SHARED}
    return config


def load_trackers(repo_root: str | Path | None = None) -> list[TrackerConfig]:
    """The enabled adapters' configs: global, with the repo's status
    mappings on top (a repo can neither enable an adapter nor name a
    credential)."""
    data = _read_toml(config_dir() / TRACKERS_FILENAME)
    repo = _read_toml(Path(repo_root) / REPO_DIR / TRACKERS_FILENAME) if repo_root else {}
    configs = []
    for name, table in data.items():
        if not isinstance(table, dict):
            continue
        config = _tracker_config(name, table)
        if not config.enabled:
            continue
        repo_table = repo.get(name)
        if isinstance(repo_table, dict):
            config.status.update(_status_rules(repo_table.get("status")))
        configs.append(config)
    return configs


def credential(config: TrackerConfig, name: str = "token") -> str | None:
    """A secret named in the global config by reference: the environment
    variable ``<name>_env``, else the OS keyring service ``keyring`` (user
    ``name``) when the ``keyring`` package is installed."""
    env = config.options.get(f"{name}_env")
    if isinstance(env, str) and os.environ.get(env):
        return os.environ[env]
    service = config.options.get("keyring")
    if isinstance(service, str) and service:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            return keyring.get_password(service, name)
        except Exception:  # noqa: BLE001 - a locked or absent backend
            return None
    return None


def branch_key(pattern: str | None, branch: str) -> str | None:
    return ticket_id(branch, pattern)


# ----------------------------------------------------------------------
# Polling
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class TrackerFact:
    """A work item's task, for the status projection."""

    adapter: str
    key: str
    url: str
    title: str
    status: str
    ball: str | None
    state: str
    level: str | None
    at: float
    stale: bool
    priority: str | None = None


@dataclass(frozen=True)
class TrackerItem:
    """An inbox item opened by a task entering a status with a ``level``."""

    id: str
    adapter: str
    key: str
    status: str
    level: str
    state: str
    title: str
    url: str
    since: float


def item_id(adapter: str, key: str, status: str) -> str:
    return f"tracker:{adapter}:{key}:{status}"


class _Feed:
    """One adapter's poll state and its last good values."""

    def __init__(self, adapter: TrackerAdapter, config: TrackerConfig) -> None:
        self.adapter = adapter
        self.config = config
        self.name = config.name
        self.pattern = config.key_pattern or adapter.key_pattern()
        self.tasks: dict[str, TrackerTask] = {}
        self.fetched: dict[str, float] = {}
        self.assigned: list[TrackerTask] | None = None
        self.assigned_ok = config.assigned_to_me
        self.last_poll = float("-inf")
        self.error: str | None = None
        self.error_at = 0.0
        self.backoff = 0.0
        self.backoff_until = 0.0
        self.calls: deque[float] = deque()

    def key(self, branch: str) -> str | None:
        return branch_key(self.pattern, branch)

    def may_call(self, now: float) -> bool:
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        return now >= self.backoff_until and len(self.calls) < self.config.max_calls_per_min

    async def call(self, now: float, coro: Any) -> Any:
        self.calls.append(now)
        try:
            result = await asyncio.wait_for(coro, CALL_TIMEOUT)
        except RateLimited as e:
            self.backoff = (
                e.retry_after
                if e.retry_after
                else min(max(self.backoff * 2, BACKOFF_MIN), BACKOFF_MAX)
            )
            self.backoff_until = now + self.backoff
            self._failed(now, str(e))
            return None
        except (TrackerError, asyncio.TimeoutError, OSError) as e:
            self._failed(now, str(e) or type(e).__name__)
            return None
        self.backoff = 0.0
        return result

    def _failed(self, now: float, message: str) -> None:
        if message != self.error:
            log.warning("tracker %s: %s", self.name, message)
        self.error, self.error_at = message, now


class TrackerHub:
    """Every enabled adapter, polled for the status projection."""

    def __init__(
        self,
        adapters: Iterable[tuple[TrackerAdapter, TrackerConfig]] = (),
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._feeds = [_Feed(a, c) for a, c in adapters]
        self.clock = clock
        # Open items by work-item key (adapter, key).
        self.items: dict[tuple[str, str], TrackerItem] = {}
        self._polling = False

    @classmethod
    def from_configs(
        cls,
        configs: Iterable[TrackerConfig],
        repo_root: str,
        *,
        factories: dict[str, AdapterFactory] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> TrackerHub:
        configs = list(configs)
        if not configs:
            return cls(clock=clock)
        factories = adapter_factories() if factories is None else factories
        adapters = []
        for config in configs:
            factory = factories.get(config.name)
            if factory is None:
                log.warning("no tracker adapter named %r", config.name)
                continue
            try:
                adapter = factory(config, repo_root)
            except Exception:  # noqa: BLE001 - one bad adapter, not the engine
                log.warning("tracker adapter %s failed", config.name, exc_info=True)
                continue
            if getattr(adapter, "experimental", False):
                log.warning(
                    "tracker adapter %s is experimental: not yet checked against "
                    "a real instance", config.name,
                )
            adapters.append((adapter, config))
        return cls(adapters, clock=clock)

    @property
    def enabled(self) -> bool:
        return bool(self._feeds)

    # --- Reading ---

    def links(self, branch: str) -> list[tuple[_Feed, str]]:
        out = []
        for feed in self._feeds:
            key = feed.key(branch)
            if key is not None:
                out.append((feed, key))
        return out

    def fact(self, branch: str | None) -> TrackerFact | None:
        """The first linked task found for ``branch``."""
        if not branch:
            return None
        for feed, key in self.links(branch):
            task = feed.tasks.get(key)
            if task is None:
                continue
            rule = feed.config.rule(task.status_raw) if task.status_raw else None
            item = self.items.get((feed.name, key))
            at = feed.fetched.get(key, 0.0)
            return TrackerFact(
                adapter=feed.name,
                key=key,
                url=task.url,
                title=task.title,
                status=task.status_raw,
                ball=rule.ball if rule is not None else None,
                state=(rule.state if rule is not None and rule.state else task.status_raw),
                level=item.level if item is not None else None,
                at=at,
                stale=feed.error is not None and feed.error_at >= at,
                priority=task.priority if task.priority in PRIORITIES else None,
            )
        return None

    def not_started(self, branches: Iterable[str]) -> int:
        """Assigned tasks that no branch links to."""
        return len(self.not_started_tasks(branches))

    def not_started_tasks(self, branches: Iterable[str]) -> list[tuple[str, TrackerTask]]:
        """``(adapter, task)`` of the assigned tasks no branch links to."""
        linked: dict[str, set[str]] = {}
        for branch in branches:
            for feed, key in self.links(branch):
                linked.setdefault(feed.name, set()).add(key)
        out = []
        for feed in self._feeds:
            keys = linked.get(feed.name, set())
            out += [(feed.name, t) for t in feed.assigned or () if t.key not in keys]
        return out

    # --- Polling ---

    async def poll(self, branches: Iterable[str], *, force: bool = False) -> bool:
        """One pass over every adapter that is due; True if anything a row
        shows changed. Runs one pass at a time."""
        if self._polling or not self._feeds:
            return False
        self._polling = True
        try:
            branches = list(branches)
            changed = False
            for feed in self._feeds:
                changed |= await self._poll_feed(feed, branches, force)
            return changed
        finally:
            self._polling = False

    async def _poll_feed(self, feed: _Feed, branches: list[str], force: bool) -> bool:
        now = self.clock()
        keys = sorted({k for b in branches if (k := feed.key(b)) is not None})
        due = force or now - feed.last_poll >= feed.config.poll_s
        wanted = keys if due else [k for k in keys if k not in feed.fetched]
        changed = self._forget(feed, set(keys))
        if not (wanted or (due and feed.assigned_ok)) or not feed.may_call(now):
            return changed
        if due:
            feed.last_poll = now
        if wanted:
            tasks = await feed.call(now, feed.adapter.fetch(wanted))
            if tasks is not None:
                changed |= self._store(feed, wanted, tasks, now)
        if due and feed.assigned_ok and feed.may_call(now):
            try:
                assigned = await feed.call(now, feed.adapter.assigned_to_me())
            except NotImplementedError:
                feed.assigned_ok = False
                assigned = None
            if assigned is not None:
                if assigned != feed.assigned:
                    changed = True
                feed.assigned = list(assigned)
        return changed

    def _store(
        self, feed: _Feed, keys: list[str], tasks: list[TrackerTask], now: float
    ) -> bool:
        found = {t.key: t for t in tasks if t.key in keys}
        # A stale mark goes away.
        changed = feed.error is not None
        feed.error = None
        for key in keys:
            task = found.get(key)
            if feed.tasks.get(key) != task:
                changed = True
                if task is None:
                    feed.tasks.pop(key, None)
                else:
                    feed.tasks[key] = task
            feed.fetched[key] = now
            changed |= self._edge(feed, key, task, now)
        return changed

    def _forget(self, feed: _Feed, keys: set[str]) -> bool:
        """Drop what no live row links to any more."""
        changed = False
        for key in [k for k in feed.fetched if k not in keys]:
            del feed.fetched[key]
            if feed.tasks.pop(key, None) is not None:
                changed = True
            changed |= self._edge(feed, key, None, 0.0)
        return changed

    def _edge(self, feed: _Feed, key: str, task: TrackerTask | None, now: float) -> bool:
        """Open an item when a task enters a status with a level; resolve it
        when it leaves."""
        rule = feed.config.rule(task.status_raw) if task and task.status_raw else None
        want = (
            item_id(feed.name, key, task.status_raw)
            if task is not None and rule is not None and rule.level
            else None
        )
        current = self.items.get((feed.name, key))
        if current is not None and current.id == want:
            return False
        if current is not None:
            del self.items[(feed.name, key)]
        if want is None or task is None or rule is None or rule.level is None:
            return current is not None
        item = TrackerItem(
            id=want,
            adapter=feed.name,
            key=key,
            status=task.status_raw,
            level=rule.level,
            state=rule.state or task.status_raw,
            title=task.title,
            url=task.url,
            since=now,
        )
        self.items[(feed.name, key)] = item
        return True
