"""Tracker adapters' core: config (global + per repo), credentials by
reference, the registry, and the hub's polling, mapping and inbox edges."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lazyagent.engine import trackers as trackers_mod
from lazyagent.engine.trackers import (
    RateLimited,
    StatusRule,
    TrackerConfig,
    TrackerError,
    TrackerHub,
    TrackerTask,
    adapter_factories,
    config_dir,
    credential,
    load_trackers,
)
from lazyagent.engine.trackers.github_issues import GithubIssues

NOW = 1_700_000_000.0

GLOBAL = """
[github_issues]
enabled = true
status_from = "labels"
poll_s = 120
[github_issues.status]
"qa" = { ball = "others", state = "in QA" }
"qa-returned" = { ball = "you", level = "round", state = "returned by QA" }
"blocked" = { ball = "you" }
"bad-ball" = { ball = "nobody" }
"bad-level" = { level = "later" }
"just-level" = { level = "now" }

[jira]
enabled = false
token_env = "JIRA_TOKEN"
"""


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- Config -----------------------------------------------------------------


def test_config_lives_in_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_dir() == tmp_path / "cfg" / "lazyagent"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config_dir() == tmp_path / ".config" / "lazyagent"


def test_no_file_no_trackers(tmp_path):
    assert load_trackers(tmp_path / "repo") == []


def test_global_config_and_its_mappings(tmp_path):
    write(config_dir() / "trackers.toml", GLOBAL)
    [config] = load_trackers(tmp_path / "repo")  # jira is off
    assert config.name == "github_issues"
    assert (config.poll_s, config.max_calls_per_min, config.assigned_to_me) == (120, 30, True)
    assert config.options == {"status_from": "labels"}
    assert config.status == {
        "qa": StatusRule("others", "in QA", None),
        "qa-returned": StatusRule("you", "returned by QA", "round"),
        "blocked": StatusRule("you", None, "round"),
        "just-level": StatusRule("you", None, "now"),
    }
    assert config.rule("QA") == config.status["qa"]
    assert config.rule("nope") is None


def test_a_repo_changes_mappings_only(tmp_path):
    write(config_dir() / "trackers.toml", GLOBAL)
    repo = tmp_path / "repo"
    write(
        repo / ".lazyagent" / "trackers.toml",
        """
[github_issues]
enabled = false
poll_s = 5
token_env = "STOLEN"
[github_issues.status]
"qa" = { ball = "others", state = "with testers" }
"ready" = { ball = "others", state = "ready" }

[jira]
enabled = true
""",
    )
    [config] = load_trackers(repo)
    assert config.enabled and config.poll_s == 120
    assert "token_env" not in config.options
    assert config.status["qa"].state == "with testers"
    assert config.status["ready"].state == "ready"
    assert "qa-returned" in config.status


def test_a_broken_file_is_ignored(tmp_path):
    write(config_dir() / "trackers.toml", "[github_issues\n")
    assert load_trackers(tmp_path) == []


def test_credentials_by_reference(monkeypatch):
    config = TrackerConfig("jira", options={"token_env": "MY_TOKEN"})
    assert credential(config) is None
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert credential(config) == "s3cret"

    stored = {}
    fake = SimpleNamespace(get_password=lambda service, name: stored.get((service, name)))
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake)
    config = TrackerConfig("jira", options={"keyring": "lazyagent/jira"})
    assert credential(config) is None
    stored[("lazyagent/jira", "token")] = "from-keyring"
    assert credential(config) == "from-keyring"


def test_registry_has_github_issues_and_entry_points(monkeypatch):
    class Plugin:
        name = "plugin"

    eps = [
        SimpleNamespace(name="plugin", load=lambda: Plugin),
        SimpleNamespace(name="github_issues", load=lambda: Plugin),
        SimpleNamespace(name="broken", load=lambda: 1 / 0),
    ]
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "entry_points",
        lambda group: eps if group == "lazyagent.trackers" else [],
    )
    factories = adapter_factories()
    assert factories["plugin"] is Plugin
    assert factories["github_issues"] is GithubIssues  # a built-in stays
    assert "broken" not in factories


def test_hub_from_configs_skips_unknown_and_failing_adapters():
    def boom(config, root):
        raise RuntimeError("no")

    hub = TrackerHub.from_configs(
        [TrackerConfig("nope", True), TrackerConfig("boom", True)],
        "/tmp/repo",
        factories={"boom": boom},
    )
    assert not hub.enabled
    assert not TrackerHub.from_configs([], "/tmp/repo").enabled


# --- The hub ----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.statuses: dict[str, str] = {}
        self.assigned: list[str] = []
        self.fetched: list[list[str]] = []
        self.assigned_calls = 0
        self.error: Exception | None = None
        self.assigned_error: Exception | None = None

    def key_pattern(self) -> str:
        return r"-(\d+)"

    def task(self, key: str) -> TrackerTask:
        return TrackerTask(key, f"https://t/{key}", f"Task {key}", self.statuses.get(key, ""))

    async def fetch(self, keys):
        self.fetched.append(list(keys))
        if self.error is not None:
            raise self.error
        return [self.task(k) for k in keys if k in self.statuses]

    async def assigned_to_me(self):
        self.assigned_calls += 1
        if self.assigned_error is not None:
            raise self.assigned_error
        return [self.task(k) for k in self.assigned]


MAP = {
    "qa": StatusRule("others", "in QA"),
    "returned": StatusRule("you", "returned by QA", "round"),
}


def hub(**config) -> tuple[TrackerHub, FakeAdapter, Clock]:
    adapter = FakeAdapter()
    clock = Clock()
    cfg = TrackerConfig("fake", True, status=dict(MAP), **config)
    return TrackerHub([(adapter, cfg)], clock=clock), adapter, clock


def run(coro):
    return asyncio.run(coro)


def test_polls_linked_keys_batched_and_maps_them():
    h, a, _ = hub()
    a.statuses = {"1": "qa", "2": "weird"}
    assert run(h.poll(["fix-1", "feat-2", "main", "fix-1-again"]))
    assert a.fetched == [["1", "2"]]
    one = h.fact("fix-1")
    assert (one.ball, one.state, one.level, one.stale) == ("others", "in QA", None, False)
    assert (one.adapter, one.key, one.url, one.at) == ("fake", "1", "https://t/1", NOW)
    # Unmapped: the raw text, the ball unchanged.
    two = h.fact("feat-2")
    assert (two.ball, two.state) == (None, "weird")
    assert h.fact("main") is None and h.fact(None) is None


def test_poll_s_at_rest_and_new_keys_at_once():
    h, a, clock = hub(poll_s=300)
    a.statuses = {"1": "qa", "2": "qa"}
    run(h.poll(["fix-1"]))
    clock.now += 60
    assert not run(h.poll(["fix-1"]))
    assert a.fetched == [["1"]]
    run(h.poll(["fix-1", "fix-2"]))
    assert a.fetched == [["1"], ["2"]]
    assert a.assigned_calls == 1
    clock.now += 300
    run(h.poll(["fix-1", "fix-2"]))
    assert a.fetched[-1] == ["1", "2"] and a.assigned_calls == 2


def test_rate_cap_per_adapter():
    h, a, clock = hub(max_calls_per_min=2, assigned_to_me=False)
    for n in range(4):
        a.statuses[str(n)] = "qa"
        run(h.poll([f"fix-{k}" for k in range(n + 1)]))
    assert a.fetched == [["0"], ["1"]]
    clock.now += 61
    run(h.poll([f"fix-{k}" for k in range(4)]))
    assert a.fetched[-1] == ["2", "3"]


def test_errors_keep_the_last_good_value_and_mark_it_stale():
    h, a, clock = hub(poll_s=60, assigned_to_me=False)
    a.statuses = {"1": "qa"}
    run(h.poll(["fix-1"]))
    a.error = TrackerError("boom")
    clock.now += 60
    assert run(h.poll(["fix-1"])) is False
    fact = h.fact("fix-1")
    assert (fact.ball, fact.at, fact.stale) == ("others", NOW, True)
    a.error = None
    clock.now += 60
    assert run(h.poll(["fix-1"]))
    assert not h.fact("fix-1").stale


def test_429_backs_off():
    h, a, clock = hub(poll_s=60, assigned_to_me=False)
    a.statuses = {"1": "qa"}
    a.error = RateLimited("slow down", retry_after=600)
    run(h.poll(["fix-1"]))
    clock.now += 300
    run(h.poll(["fix-1"]))
    assert len(a.fetched) == 1
    clock.now += 301
    a.error = RateLimited("again")  # no Retry-After: twice the last wait
    run(h.poll(["fix-1"]))
    assert len(a.fetched) == 2
    clock.now += 1199
    run(h.poll(["fix-1"]))
    assert len(a.fetched) == 2
    clock.now += 1
    a.error = None
    run(h.poll(["fix-1"]))
    assert len(a.fetched) == 3 and h._feeds[0].backoff == 0

    a.error = RateLimited("first")  # from a minute
    clock.now += 60
    run(h.poll(["fix-1"]))
    assert h._feeds[0].backoff == 60


def test_a_slow_adapter_times_out(monkeypatch):
    monkeypatch.setattr(trackers_mod, "CALL_TIMEOUT", 0.01)
    h, a, _ = hub(assigned_to_me=False)

    async def slow(keys):
        await asyncio.sleep(1)

    a.fetch = slow
    assert run(h.poll(["fix-1"])) is False
    assert h._feeds[0].error


def test_inbox_items_on_entering_a_level_status_and_resolved_on_leaving():
    h, a, clock = hub(poll_s=60, assigned_to_me=False)
    opened = []

    def step(statuses=None, branches=("fix-1",)):
        if statuses is not None:
            a.statuses = statuses
        clock.now += 60
        run(h.poll(list(branches)))
        item = h.items.get(("fake", "1"))
        opened.append(None if item is None else (item.id, item.since))

    step({"1": "qa"})
    step({"1": "returned"})
    item = h.items[("fake", "1")]
    assert item.id == "tracker:fake:1:returned"
    assert (item.level, item.state, item.url) == ("round", "returned by QA", "https://t/1")
    assert h.fact("fix-1").level == "round"

    # Still in it: the same item (edge, not level).
    for _ in range(3):
        step()
    step({"1": "qa"})
    assert h.fact("fix-1").level is None
    # Entering again is a new edge; the branch going away resolves it.
    step({"1": "returned"})
    step(branches=())
    entered = ("tracker:fake:1:returned", NOW + 120)
    again = ("tracker:fake:1:returned", NOW + 420)
    assert opened == [None, entered, entered, entered, entered, None, again, None]
    assert h.fact("fix-1") is None


def test_not_started_counts_assigned_tasks_with_no_branch():
    h, a, _ = hub()
    a.assigned = ["1", "2", "3"]
    run(h.poll(["fix-1"]))
    assert h.not_started(["fix-1"]) == 2
    assert h.not_started(["fix-1", "feat-3", "main"]) == 1


def test_assigned_to_me_is_optional():
    h, a, clock = hub()
    a.assigned_error = NotImplementedError()
    run(h.poll(["fix-1"]))
    clock.now += 1000
    run(h.poll(["fix-1"]))
    assert a.assigned_calls == 1
    assert h.not_started([]) == 0


def test_a_disabled_hub_does_nothing():
    h = TrackerHub()
    assert not h.enabled
    assert run(h.poll(["fix-1"])) is False
    assert h.fact("fix-1") is None and h.not_started(["x"]) == 0


@pytest.mark.parametrize(
    "pattern, branch, key", [(None, "ABC-12-x", "ABC-12"), (r"#(\d+)", "a#9", "9")]
)
def test_key_pattern_from_config_overrides_the_adapter(pattern, branch, key):
    class NoPattern(FakeAdapter):
        def key_pattern(self):
            return None

    h = TrackerHub([(NoPattern(), TrackerConfig("fake", True, key_pattern=pattern))])
    assert h.links(branch)[0][1] == key
