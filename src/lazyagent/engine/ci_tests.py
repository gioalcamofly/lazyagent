"""CI red but unrelated to the branch, mechanism A (proposal 0014 "CI red but
unrelated to the branch"; docs/design/rules-plan.md R4).

A red check counts as **unrelated** only when its failing **tests** (not the
check's colour) all fail on the base too, at the base point the branch is
compared with::

    unrelated(c)  ⇔  branch_failing(c, H) is known and non-empty
                   ∧ base_failing(c, P) is known
                   ∧ branch_failing(c, H) ⊆ base_failing(c, P)

Anything else is **related**, the conservative default: no test data (no
``tests_from`` for the check, no artifact, a parser's count check failed, a
build or lint failure), no base run at exactly *P*, a check that exists only
on the branch, a check that is not an Actions run. A check red on both
sides with different failing tests is related: at least one failure is the
branch's.

**Check identity.** ``<workflow file>/<job name>``, the matrix values in the
job's name included (``.github/workflows/ci.yml/unit-tests (3.12)``), never
the display name alone.

**Test ids.** ``<check id>::<suite or file>::<name>``, parameters kept,
worker ids (``[gw3]``), durations and colour codes stripped; per check, from
the sources ``[ci.check."<name>"] tests_from`` lists, the first that gives a
known set: ``junit:<artifact glob>`` (the run's artifacts, every ``*.xml``'s
failing ``<testcase>``, read from the zip one file at a time and checked
against the files' own counts), ``log:<parser>`` (the failed log's head and
tail, read by a built-in parser that must find as many failures as the
runner's own totals line says), ``log:regex`` (the user's RE2
``test_regex`` and ``total_regex``). The base must come from the same
source as the branch.

**Aggregate jobs** (``aggregate = true``, or with ``[ci_unrelated]
detect_aggregates = true`` detected: no ``tests_from``, started after the
run's other failing jobs ended, at most two steps of its own) have no tests:
one is unrelated when the run's other failing jobs all are.

**The base point P.** ``compare_to = "build_base"`` (default): for a
``pull_request`` run, the base parent of the merge ref the run built
(``refs/pull/<n>/merge``, recorded when the engine first sees the run, and
only while one parent is the run's head); for a ``push`` run, ``git
merge-base --all <head> origin/<base>`` (one merge-base, else unknown); any
other event, unknown. ``compare_to = "<ref>"`` (per repo, or per I6
project): the newest commit of the ref that is an ancestor of the build
base. The base data is the base branch's own ``push`` run of the same
workflow **at exactly P**, never the nearest one.

**Cost.** Nothing at all until a check has ``tests_from``. Then, only while
a head is red: one artifact list and one download (or the failed log the
summary reads anyway) per failing job per run attempt, the same for the
base run, cached per base sha (one base run serves every branch on it), all
inside the Actions hub's caps, at most :data:`MAX_FETCHES` a poll. The
evaluator never waits: until the data is in, a check is related.

**Sync with base** (``ci.base_fixed``): for rows where every failing check
is unrelated, the newest run of that workflow on the base branch in the
runs the poll already holds. A green one says the base was fixed with no
further call; a red one has its test data read lazily, at most
``base_fetches_per_poll`` (3) a poll.

What was decided is kept in ``$XDG_STATE_HOME/lazyagent/ci_tests/<repo
hash>.json``, so a restart does not bring a cleared Fix CI back while the
data is read again.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lazyagent.engine.infra_rerun import DEFAULT_MAX_ATTEMPTS
from lazyagent.engine.trackers import TrackerError
from lazyagent.models import WorkflowJob, WorkflowRun

if TYPE_CHECKING:
    from lazyagent.engine.actions import ActionsHub, CiVerdict
    from lazyagent.engine.required_checks import Required
    from lazyagent.models import PrInfo

log = logging.getLogger(__name__)

BUILD_BASE = "build_base"
DEFAULT_BASE_FETCHES = 3
# Test-data reads one poll may start (artifact lists, downloads, logs, base
# runs, merge refs), inside the hub's 20 calls a minute.
MAX_FETCHES = 8
# JUnit: an artifact (a zip) larger than this is not downloaded. Its XML
# files are streamed one at a time, however many: each is scanned for
# ``<failure`` / ``<error`` and its testsuite counts, and only those with a
# failure are parsed. Caps: the XML scanned in all, one file, the XML
# parsed, the test cases parsed, the seconds spent.
JUNIT_MAX_BYTES = 20 * 1024 * 1024
JUNIT_MAX_XML_BYTES = 512 * 1024 * 1024
JUNIT_MAX_FILE_BYTES = 32 * 1024 * 1024
JUNIT_MAX_PARSE_BYTES = 64 * 1024 * 1024
JUNIT_MAX_CASES = 50_000
JUNIT_MAX_SECONDS = 30.0
JUNIT_TIMEOUT = 120.0
# A user regex runs on lines cut to this many characters.
REGEX_LINE_CHARS = 300
# Decided entries kept (job results, base points, base runs).
CACHE = 512
# An aggregate job found without config runs at most this many steps of
# its own.
AGGREGATE_STEPS = 2
# Polls a failed read is tried on before the check stays related.
RETRIES = 3
SOURCES = ("junit", "log")

# argv (after "git") → (exit code, stdout)
Git = Callable[[list[str]], tuple[int, str]]


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------

_MATRIX = re.compile(r"^(.*\S)\s+\(([^()]*)\)$")


def check_id(run: WorkflowRun, job: str) -> str:
    """``<workflow file>/<job name>``; the workflow's name when the run has
    no path."""
    return f"{run.path or run.workflow}/{job}"


def bare_job(job: str) -> str:
    """The job's name without its matrix values: ``unit-tests (3.12)`` →
    ``unit-tests``."""
    m = _MATRIX.match(job)
    return m[1] if m else job


def matrix_values(job: str) -> tuple[str, ...]:
    m = _MATRIX.match(job)
    return tuple(v.strip() for v in m[2].split(",") if v.strip()) if m else ()


# ----------------------------------------------------------------------
# Config ([ci.check."<name>"], [ci_unrelated])
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckConfig:
    """``[ci.check."<name>"]``: where a check's failing-test ids come from.
    ``name`` matches a check id, a job name or a job name without its matrix
    values, ``*`` and ``?`` allowed."""

    name: str
    tests_from: tuple[str, ...]
    test_regex: str | None = None
    total_regex: str | None = None
    # An aggregate job (it waits on the run's other jobs and has no tests
    # of its own): True, False, or None to detect it.
    aggregate: bool | None = None
    # R4c: not a merge gate (its red alone raises no Fix CI, unless its
    # failing tests are the branch's), or a process check (a label policy:
    # an item only once the merge is next).
    gating: bool = True
    policy: bool = False
    # The table says ``gating`` itself (R5a: it wins over the gating read
    # from branch protection).
    gating_set: bool = False

    @property
    def gate(self) -> str:
        """``policy``, ``nongating`` or ``gating``."""
        return "policy" if self.policy else ("gating" if self.gating else "nongating")

    def matches(self, check: str, job: str) -> bool:
        return any(fnmatch.fnmatchcase(x, self.name) for x in (check, job, bare_job(job)))


@dataclass(frozen=True)
class AttestRules:
    """What an attestation for a nondeterministic check must show (proposal
    0014 mechanism B, engine/attest.py): ``[ci]`` defaults, per check
    ``[ci.check."<name>"]``."""

    confirm: str = "user"
    min_base_runs: int = 5
    min_repetitions: int = 3
    max_drop: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        return {"confirm": self.confirm, "min_base_runs": self.min_base_runs,
                "min_repetitions": self.min_repetitions, "max_drop": self.max_drop}


ATTEST_DEFAULTS = AttestRules()
DEFAULT_ATTESTATION_TTL = 72 * 3600


def _matches(pattern: str, check: str, job: str) -> bool:
    return any(fnmatch.fnmatchcase(x.lower(), pattern.lower())
               for x in (check, job, bare_job(job)))


@dataclass(frozen=True)
class CiConfig:
    """The effective ``[ci]``, ``[ci.check]`` and ``[ci_unrelated]`` of the
    rule files (repo over global)."""

    checks: tuple[CheckConfig, ...] = ()
    # The repo file's, else the global file's, else build_base; with where
    # it was set ("repo", "global", "").
    compare_to: str = BUILD_BASE
    compare_from: str = ""
    # I6 project id → compare_to (the repo's over the global's).
    projects: Mapping[str, str] = field(default_factory=dict)
    base_fetches_per_poll: int = DEFAULT_BASE_FETCHES
    # Take a job for an aggregate from the run's jobs when its check does
    # not say (opt-in: a wrong "unrelated" could let a real failure merge).
    detect_aggregates: bool = False
    # Mechanism B (R5): the checks whose pass rate varies run to run, those
    # an accepted attestation explains, how long one lasts, and what one
    # must show ([ci] defaults; per check name, its own keys only).
    nondeterministic: tuple[str, ...] = ()
    accept_attestation_for: tuple[str, ...] = ()
    attestation_ttl: int = DEFAULT_ATTESTATION_TTL
    attest_defaults: AttestRules = ATTEST_DEFAULTS
    attest_checks: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # R5a: which checks gate comes from branch protection (engine/
    # required_checks.py) unless a [ci.check] says; infrastructure failures
    # are re-run (engine/infra_rerun.py) up to this many attempts.
    gating_from_protection: bool = True
    rerun_infra: bool = True
    rerun_infra_max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @property
    def configured(self) -> bool:
        return any(c.tests_from or c.gate != "gating" or c.gating_set for c in self.checks) \
            or bool(self.nondeterministic)

    @property
    def gates_set(self) -> bool:
        """A [ci.check] says gating or policy: the user's own list."""
        return any(c.gating_set or c.policy for c in self.checks)

    def check(self, check: str, job: str) -> CheckConfig | None:
        return next((c for c in self.checks if c.matches(check, job)), None)

    def gate_of(self, check: str, job: str = "") -> str:
        """The check's gate (R4c): ``gating`` unless its table says."""
        c = self.check(check, job or check)
        return c.gate if c is not None else "gating"

    def own_gate(self, check: str, job: str = "") -> str | None:
        """The gate a [ci.check] table sets itself (``gating = true/false``,
        ``policy = true``); None: the table does not say."""
        c = self.check(check, job or check)
        return c.gate if c is not None and (c.gating_set or c.policy) else None

    def tests_for(self, check: str, job: str) -> CheckConfig | None:
        """The check's config when it has ``tests_from``."""
        c = self.check(check, job)
        return c if c is not None and c.tests_from else None

    def is_nondeterministic(self, check: str, job: str) -> bool:
        return any(_matches(p, check, job) for p in self.nondeterministic)

    def accepts(self, check: str, job: str) -> bool:
        return any(_matches(p, check, job) for p in self.accept_attestation_for)

    def attest_rules(self, check: str, job: str) -> AttestRules:
        own = next((v for k, v in self.attest_checks.items() if _matches(k, check, job)), {})
        return replace(self.attest_defaults, **own) if own else self.attest_defaults

    def compare_for(self, project: str | None) -> tuple[str, str]:
        """``(compare_to, why)``: the project's, else the files'."""
        if project and project in self.projects:
            return self.projects[project], f"project {project}"
        return self.compare_to, (f"{self.compare_from} rules.toml" if self.compare_from else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [
                {"name": c.name, "tests_from": list(c.tests_from)}
                | ({"aggregate": c.aggregate} if c.aggregate is not None else {})
                | ({"test_regex": c.test_regex} if c.test_regex else {})
                | ({"total_regex": c.total_regex} if c.total_regex else {})
                | ({"gating": c.gating} if c.gating_set else {})
                | ({"policy": True} if c.policy else {})
                for c in self.checks
            ],
            "compare_to": self.compare_to,
            "projects": dict(sorted(self.projects.items())),
            "base_fetches_per_poll": self.base_fetches_per_poll,
            "nondeterministic": list(self.nondeterministic),
            "accept_attestation_for": list(self.accept_attestation_for),
            "attestation_ttl": self.attestation_ttl,
            "attest": self.attest_defaults.to_dict(),
            "attest_checks": {k: dict(v) for k, v in sorted(self.attest_checks.items())},
        } | ({"detect_aggregates": True} if self.detect_aggregates else {}) \
          | {k: getattr(self, k) for k in OPTION_KEYS if getattr(self, k) != getattr(NO_CONFIG, k)}


NO_CONFIG = CiConfig()

# R5a's [ci] switches.
OPTION_KEYS = ("gating_from_protection", "rerun_infra", "rerun_infra_max_attempts")
CI_KEYS = frozenset({
    "check", "nondeterministic", "accept_attestation_for", "attestation_ttl",
    "min_base_runs", "min_repetitions", "max_drop", *OPTION_KEYS,
})
ATTEST_KEYS = frozenset({"confirm", "min_base_runs", "min_repetitions", "max_drop"})
CHECK_KEYS = frozenset({"tests_from", "test_regex", "total_regex", "aggregate", "gating",
                        "policy"}) | ATTEST_KEYS
UNRELATED_KEYS = frozenset({"compare_to", "base_fetches_per_poll", "project",
                            "detect_aggregates"})
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


@dataclass
class CiPart:
    """One rule file's ``[ci]`` and ``[ci_unrelated]`` (user.parse_file)."""

    checks: list[CheckConfig] = field(default_factory=list)
    compare_to: str | None = None
    projects: dict[str, str] = field(default_factory=dict)
    base_fetches_per_poll: int | None = None
    detect_aggregates: bool | None = None
    # Mechanism B: each [ci] key the file sets, and per check name its own
    # attestation keys.
    attest: dict[str, Any] = field(default_factory=dict)
    attest_checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # R5a: gating_from_protection, rerun_infra, rerun_infra_max_attempts.
    options: dict[str, Any] = field(default_factory=dict)


_TTL = re.compile(r"^(\d+)\s*([smhd])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _names(key: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f'{key} must be a list of check names, such as ["eval-suite"]')
    return tuple(v.strip() for v in value)


def _ttl(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    m = _TTL.match(value.strip()) if isinstance(value, str) else None
    if m is None or int(m[1]) <= 0:
        raise ValueError('attestation_ttl must be a duration such as "72h" or "3d" (or seconds)')
    return int(m[1]) * _UNITS[m[2]]


def _option(key: str, value: Any) -> Any:
    if key == "rerun_infra_max_attempts":
        if type(value) is not int or not 1 <= value <= 10:
            raise ValueError("rerun_infra_max_attempts must be a number from 1 to 10")
        return value
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _attest_value(key: str, value: Any) -> Any:
    """One attestation key's value, checked (ValueError)."""
    if key == "confirm":
        if value not in ("user", "none"):
            raise ValueError('confirm must be "user" (the default) or "none"')
        return value
    if key in ("min_base_runs", "min_repetitions"):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError(f"{key} must be a number from 1 to 100")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError("max_drop must be a fraction from 0 to 1, such as 0.05")
    return float(value)


def parse_tables(ci: Any, unrelated: Any) -> tuple[CiPart, list[str], list[str]]:
    """``[ci]`` (its ``check`` tables) and ``[ci_unrelated]`` →
    ``(part, errors, warnings)``; a bad entry is left out alone."""
    part, errors, warnings = CiPart(), [], []
    if ci is not None:
        if not isinstance(ci, dict):
            errors.append("[ci] must be a table")
        else:
            for key in sorted(set(ci) - CI_KEYS):
                errors.append(f"unknown key {key!r} in [ci]")
            for key in sorted(set(ci) & (CI_KEYS - {"check"})):
                try:
                    if key in OPTION_KEYS:
                        part.options[key] = _option(key, ci[key])
                    elif key in ("nondeterministic", "accept_attestation_for"):
                        part.attest[key] = _names(f"[ci] {key}", ci[key])
                    elif key == "attestation_ttl":
                        part.attest[key] = _ttl(ci[key])
                    else:
                        part.attest[key] = _attest_value(key, ci[key])
                except ValueError as e:
                    errors.append(f"[ci] {e}")
            checks = ci.get("check", {})
            if not isinstance(checks, dict):
                errors.append('[ci] check must be tables ([ci.check."<name>"])')
                checks = {}
            for name, table in checks.items():
                where = f'[ci.check."{name}"]'
                try:
                    check = _check_config(name, table, where, warnings)
                    own = {k: _attest_value(k, table[k]) for k in sorted(ATTEST_KEYS & set(table))}
                except ValueError as e:
                    errors.append(f"{where}: {e}")
                    continue
                if check is not None:
                    part.checks.append(check)
                if own:
                    part.attest_checks[name] = own
    if unrelated is not None:
        if not isinstance(unrelated, dict):
            errors.append("[ci_unrelated] must be a table")
            return part, errors, warnings
        for key in sorted(set(unrelated) - UNRELATED_KEYS):
            errors.append(f"unknown key {key!r} in [ci_unrelated]")
        if "compare_to" in unrelated:
            try:
                part.compare_to = _compare_to(unrelated["compare_to"])
            except ValueError as e:
                errors.append(f"[ci_unrelated] compare_to: {e}")
        if "base_fetches_per_poll" in unrelated:
            n = unrelated["base_fetches_per_poll"]
            if type(n) is not int or not 0 <= n <= 20:
                errors.append("[ci_unrelated] base_fetches_per_poll must be a number from 0 to 20")
            else:
                part.base_fetches_per_poll = n
        if "detect_aggregates" in unrelated:
            if not isinstance(unrelated["detect_aggregates"], bool):
                errors.append("[ci_unrelated] detect_aggregates must be true or false")
            else:
                part.detect_aggregates = unrelated["detect_aggregates"]
        projects = unrelated.get("project", {})
        if not isinstance(projects, dict):
            errors.append('[ci_unrelated] project must be tables ([ci_unrelated.project."<id>"])')
            projects = {}
        for pid, table in projects.items():
            where = f'[ci_unrelated.project."{pid}"]'
            if not isinstance(table, dict) or set(table) - {"compare_to"} or "compare_to" not in table:
                errors.append(f"{where} takes compare_to only")
                continue
            try:
                part.projects[pid] = _compare_to(table["compare_to"])
            except ValueError as e:
                errors.append(f"{where} compare_to: {e}")
    return part, errors, warnings


def _compare_to(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('"build_base" or a ref such as "origin/main"')
    if value == BUILD_BASE:
        return value
    if not _REF.match(value) or ".." in value or value.endswith((".lock", "/")):
        raise ValueError(f"{value!r} is not a ref name")
    return value


def _check_config(name: Any, table: Any, where: str, warnings: list[str]) -> CheckConfig | None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a check name is required")
    if not isinstance(table, dict):
        raise ValueError("must be a table")
    for key in sorted(set(table) - CHECK_KEYS):
        raise ValueError(f"unknown key {key!r}")
    aggregate = table.get("aggregate")
    if aggregate is not None and not isinstance(aggregate, bool):
        raise ValueError("aggregate must be true or false")
    for key in ("gating", "policy"):
        if key in table and not isinstance(table[key], bool):
            raise ValueError(f"{key} must be true or false")
    gating, policy = table.get("gating", True), table.get("policy", False)
    if policy and table.get("gating") is True:
        raise ValueError("a policy check is not a merge gate: drop gating = true")
    gates = dict(gating=gating, policy=policy, gating_set="gating" in table)
    sources = table.get("tests_from")
    if aggregate and sources is not None:
        raise ValueError("an aggregate job has no tests of its own: drop tests_from")
    if sources is None:
        if aggregate is None and gating and not policy and "gating" not in table:
            return None
        if "test_regex" in table or "total_regex" in table:
            raise ValueError("test_regex and total_regex are for log:regex")
        return CheckConfig(name, (), aggregate=aggregate, **gates)
    if not isinstance(sources, list) or not sources or not all(isinstance(s, str) for s in sources):
        raise ValueError('tests_from must be a list such as ["junit:junit-*", "log:pytest"]')
    for s in sources:
        kind, _, arg = s.partition(":")
        if kind == "junit" and arg:
            continue
        if kind == "log" and (arg in PARSERS or arg == "regex"):
            continue
        hint = ", ".join(f"log:{p}" for p in (*PARSERS, "regex"))
        raise ValueError(f"tests_from {s!r}: write junit:<artifact name glob> or one of {hint}")
    test_regex, total_regex = table.get("test_regex"), table.get("total_regex")
    if "log:regex" in sources:
        for key, value, group in (("test_regex", test_regex, "test"),
                                  ("total_regex", total_regex, "total")):
            if not isinstance(value, str) or not value:
                raise ValueError(f"log:regex needs {key} (a named group (?P<{group}>...))")
            compile_re2(value, group)
    elif test_regex is not None or total_regex is not None:
        raise ValueError("test_regex and total_regex are for log:regex")
    return CheckConfig(name, tuple(sources), test_regex, total_regex, aggregate, **gates)


def merge_parts(parts: Iterable[tuple[str, CiPart]]) -> CiConfig:
    """The files' parts, highest precedence first (repo, then global)."""
    checks: list[CheckConfig] = []
    compare_to, compare_from, fetches, detect = None, "", None, None
    projects: dict[str, str] = {}
    attest: dict[str, Any] = {}
    attest_checks: dict[str, dict[str, Any]] = {}
    options: dict[str, Any] = {}
    for scope, part in parts:
        if detect is None:
            detect = part.detect_aggregates
        names = {c.name for c in checks}
        checks += [c for c in part.checks if c.name not in names]
        if compare_to is None and part.compare_to is not None:
            compare_to, compare_from = part.compare_to, scope
        if fetches is None:
            fetches = part.base_fetches_per_poll
        for pid, ref in part.projects.items():
            projects.setdefault(pid, ref)
        for key, value in part.attest.items():
            attest.setdefault(key, value)
        for name, own in part.attest_checks.items():
            attest_checks.setdefault(name, own)
        for key, value in part.options.items():
            options.setdefault(key, value)
    if not checks and compare_to is None and not projects and fetches is None \
            and detect is None and not attest and not attest_checks and not options:
        return NO_CONFIG
    defaults = AttestRules(**{k: attest[k] for k in ATTEST_KEYS - {"confirm"} if k in attest})
    return CiConfig(tuple(checks), compare_to or BUILD_BASE, compare_from, projects,
                    DEFAULT_BASE_FETCHES if fetches is None else fetches, bool(detect),
                    nondeterministic=attest.get("nondeterministic", ()),
                    accept_attestation_for=attest.get("accept_attestation_for", ()),
                    attestation_ttl=attest.get("attestation_ttl", DEFAULT_ATTESTATION_TTL),
                    attest_defaults=defaults, attest_checks=attest_checks, **options)


# ----------------------------------------------------------------------
# User regexes: RE2 only
# ----------------------------------------------------------------------

# What RE2 does not have: lookaround, backreferences, atomic groups,
# conditionals, possessive quantifiers.
_NOT_RE2 = re.compile(
    r"\(\?(?:=|!|<=|<!|>|\(|P=)|\\[1-9]|\\k<|[*+?}]\+"
)


def compile_re2(pattern: str, group: str) -> Any:
    """The pattern, with ``google-re2`` when it is installed, else Python's
    ``re`` after a check that it stays inside RE2's syntax. Raises
    ValueError."""
    try:
        import re2  # type: ignore[import-not-found]
    except ImportError:
        re2 = None
    if re2 is None and _NOT_RE2.search(pattern):
        raise ValueError(f"{pattern!r}: only RE2 syntax (no lookaround, no backreferences)")
    try:
        compiled = (re2 or re).compile(pattern)
    except Exception as e:  # re.error, re2.error
        raise ValueError(f"{pattern!r}: {e}") from None
    if group not in compiled.groupindex:
        raise ValueError(f"{pattern!r} has no named group (?P<{group}>...)")
    return compiled


# ----------------------------------------------------------------------
# Failing-test ids
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    """A check's failing-test ids on one run, or why they are unknown."""

    ids: frozenset[str] | None
    source: str
    why: str

    @property
    def known(self) -> bool:
        return self.ids is not None

    def to_json(self) -> dict[str, Any]:
        return {"ids": None if self.ids is None else sorted(self.ids), "source": self.source,
                "why": self.why}

    @staticmethod
    def from_json(d: dict[str, Any]) -> Found:
        ids = d.get("ids")
        return Found(None if ids is None else frozenset(ids), str(d.get("source", "")),
                     str(d.get("why", "")))


def unknown(source: str, why: str) -> Found:
    return Found(None, source, why)


def _known(source: str, ids: Iterable[str], why: str) -> Found:
    ids = frozenset(ids)
    if not ids:
        return unknown(source, f"{source}: no failing test in it (a build or setup failure?)")
    return Found(ids, source, why)


_WORKER = re.compile(r"^\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?")
_DURATION = re.compile(r"\s+\(\d+(?:\.\d+)?\s*(?:ms|s|sec|seconds)\)$")


def _norm(test: str) -> str:
    return _DURATION.sub("", _WORKER.sub("", " ".join(test.split())))


def _counted(source: str, ids: set[str], expected: int | None, what: str = "failures") -> Found:
    """The count check: the ids found must be the runner's own number."""
    if expected is None:
        return unknown(source, f"{source}: no totals line (the log may be cut)")
    if len(ids) != expected:
        return unknown(source, f"{source} parser found {len(ids)} of {expected} {what}")
    n = len(ids)
    return _known(source, ids, f"{source}: {n} failing test{'s' if n != 1 else ''}")


_PYTEST_ID = re.compile(r"^(?:\[gw\d+\]\s+(?:\[\s*\d+%\]\s+)?)?(FAILED|ERROR)\s+(.+?)(?:\s+-\s.*)?$")
_PYTEST_TOTALS = re.compile(r"^=+\s+(.*?)\s+in\s+[\d.]+s(?:\s+\([^)]*\))?\s+=+$")
_COUNT = re.compile(r"(\d+)\s+(failed|errors?|passed|skipped|xfailed|xpassed|deselected|warnings?)")


def parse_pytest(lines: list[str]) -> Found:
    """``FAILED <nodeid> - …`` / ``ERROR <nodeid>`` summary lines; the
    totals line ``= 2 failed, 120 passed in 3.1s =`` (failed + errors)."""
    ids: set[str] = set()
    expected = None
    for line in lines:
        s = line.strip()
        m = _PYTEST_TOTALS.match(s)
        if m and _COUNT.search(m[1]):
            counts: dict[str, int] = {}
            for n, what in _COUNT.findall(m[1]):
                key = "error" if what.startswith("error") else what
                counts[key] = counts.get(key, 0) + int(n)
            expected = counts.get("failed", 0) + counts.get("error", 0)
            continue
        m = _PYTEST_ID.match(s)
        if m and ("::" in m[2] or m[1] == "ERROR"):
            ids.add(_norm(m[2]))
    return _counted("pytest", ids, expected)


_JEST_FAIL = re.compile(r"^FAIL\s+(\S+)")
_JEST_TEST = re.compile(r"^\s*●\s+(.+?)\s*$")
_JEST_TOTALS = re.compile(r"^\s*Tests:\s+(\d+)\s+failed")


def parse_jest(lines: list[str]) -> Found:
    """``●`` test names under ``FAIL <file>``; ``Tests: 2 failed, …``."""
    ids: set[str] = set()
    expected, current = None, None
    for line in lines:
        if line is CUT:
            # The FAIL line above may not be this test's.
            current = None
            continue
        m = _JEST_FAIL.match(line.strip())
        if m:
            current = m[1]
            continue
        m = _JEST_TOTALS.match(line)
        if m:
            expected = int(m[1])
            continue
        m = _JEST_TEST.match(line)
        if m and current is not None:
            if m[1].startswith("Test suite failed to run"):
                return unknown("jest", f"jest: the suite {current} failed to run")
            if m[1].startswith("Console"):
                continue
            ids.add(f"{current}::{_norm(m[1])}")
    return _counted("jest", ids, expected)


_VITEST_FAIL = re.compile(r"^FAIL\s+(\S+)\s+>\s+(.+?)\s*$")
_VITEST_SUITE = re.compile(r"^FAIL\s+(\S+)\s+\[")
_VITEST_TOTALS = re.compile(r"^Tests\s+(\d+)\s+failed")


def parse_vitest(lines: list[str]) -> Found:
    """`` FAIL  <file> > suite > name`` lines; ``Tests  1 failed | 2 passed``."""
    ids: set[str] = set()
    expected = None
    for line in lines:
        s = line.strip()
        if _VITEST_SUITE.match(s):
            return unknown("vitest", "vitest: a test file failed to run")
        m = _VITEST_FAIL.match(s)
        if m:
            ids.add(f"{m[1]}::{_norm(m[2])}")
            continue
        m = _VITEST_TOTALS.match(s)
        if m:
            expected = int(m[1])
    return _counted("vitest", ids, expected)


_GO_TEST = re.compile(r"^\s*--- FAIL:\s+(\S+)")
_GO_PKG_FAIL = re.compile(r"^FAIL\s+(\S+)\s+(?:[\d.]+s|\(cached\))\s*$")
_GO_PKG_BROKEN = re.compile(r"^FAIL\s+(\S+)\s+\[(build|setup) failed\]")
_GO_OK = re.compile(r"^ok\s+\S+")
_GO_DONE = re.compile(r"^DONE\s+(\d+)\s+tests?(?:,\s+\d+\s+skipped)?,\s+(\d+)\s+failures?")


def parse_go(lines: list[str]) -> Found:
    """``--- FAIL: TestX (…)`` (subtests kept), each package's ``FAIL\\t<pkg>``
    line. ``go test`` prints no count of failed tests: every failed package
    must show a failing test, and gotestsum's ``DONE N tests, M failures``
    is checked when it is there."""
    ids: set[str] = set()
    pending: list[str] = []
    packages = 0
    expected = None
    for line in lines:
        s = line.rstrip()
        m = _GO_TEST.match(s)
        if m:
            pending.append(m[1])
            continue
        if s.startswith("panic: "):
            return unknown("go", "go: a test panicked")
        m = _GO_PKG_BROKEN.match(s)
        if m:
            return unknown("go", f"go: {m[1]}: {m[2]} failed")
        m = _GO_PKG_FAIL.match(s)
        if m:
            if not pending:
                return unknown("go", f"go: {m[1]} failed with no failing test")
            ids.update(f"{m[1]}::{t}" for t in pending)
            pending, packages = [], packages + 1
            continue
        if _GO_OK.match(s):
            pending = []
            continue
        m = _GO_DONE.match(s)
        if m:
            expected = int(m[2])
    if pending:
        return unknown("go", "go: failing tests with no FAIL line for their package (the log may be cut)")
    if expected is not None:
        return _counted("go", ids, expected)
    if not packages:
        return unknown("go", "go: no failed package (the log may be cut)")
    n = len(ids)
    return _known("go", ids, f"go: {n} failing test{'s' if n != 1 else ''} in {packages} "
                             f"package{'s' if packages != 1 else ''}")


_CARGO_RUNNING = re.compile(r"^\s*Running\s+(?:unittests\s+)?(\S+)(?:\s+\(([^)]*)\))?")
_CARGO_TEST = re.compile(r"^test (.+) \.\.\. FAILED$")
_CARGO_RESULT = re.compile(r"^test result: \w+\.\s+(\d+) passed;\s+(\d+) failed;")
_CARGO_HASH = re.compile(r"-[0-9a-f]{8,}$")


def parse_cargo(lines: list[str]) -> Found:
    """``test <path> ... FAILED`` per test binary (``Running …``); the sum of
    the ``test result: … N failed;`` lines."""
    ids: set[str] = set()
    expected = None
    binary = ""
    for line in lines:
        s = line.strip()
        m = _CARGO_RUNNING.match(line)
        if m:
            target = m[2] or m[1]
            binary = _CARGO_HASH.sub("", os.path.basename(target))
            continue
        m = _CARGO_TEST.match(s)
        if m:
            ids.add(f"{binary}::{m[1]}" if binary else m[1])
            continue
        m = _CARGO_RESULT.match(s)
        if m:
            expected = (expected or 0) + int(m[2])
    return _counted("cargo", ids, expected)


_MVN_OLD = re.compile(r"^\[ERROR\]\s+(\S+?)\((\S+)\)\s+(?:Time elapsed.*)?<<<\s*(?:FAILURE|ERROR)!")
_MVN_NEW = re.compile(r"^\[ERROR\]\s+(\S+)\.([^.\s]+)\s+--\s+Time elapsed.*<<<\s*(?:FAILURE|ERROR)!")
_MVN_TOTALS = re.compile(r"^\[(?:ERROR|WARNING|INFO)\]\s+Tests run:\s*(\d+),\s*Failures:\s*(\d+),"
                         r"\s*Errors:\s*(\d+)")
_GRADLE_TEST = re.compile(r"^(\S+) > (.+) FAILED$")
_GRADLE_TOTALS = re.compile(r"^(\d+) tests? completed, (\d+) failed")


def parse_junit_console(lines: list[str]) -> Found:
    """Maven Surefire (``testX(com.a.FooTest) … <<< FAILURE!``, the
    aggregate ``Tests run: N, Failures: F, Errors: E``) or Gradle (``FooTest
    > testX FAILED``, ``N tests completed, F failed``)."""
    ids: set[str] = set()
    expected = None
    for line in lines:
        s = line.strip()
        m = _MVN_TOTALS.match(s)
        if m:
            if " in " not in s:
                expected = (expected or 0) + int(m[2]) + int(m[3])
            continue
        m = _MVN_OLD.match(s)
        if m:
            ids.add(f"{m[2]}::{m[1]}")
            continue
        m = _MVN_NEW.match(s)
        if m:
            ids.add(f"{m[1]}::{m[2]}")
            continue
        m = _GRADLE_TEST.match(s)
        if m:
            ids.add(f"{m[1]}::{_norm(m[2])}")
            continue
        m = _GRADLE_TOTALS.match(s)
        if m:
            expected = (expected or 0) + int(m[2])
    return _counted("junit-console", ids, expected)


PARSERS: dict[str, Callable[[list[str]], Found]] = {
    "pytest": parse_pytest,
    "jest": parse_jest,
    "vitest": parse_vitest,
    "go": parse_go,
    "cargo": parse_cargo,
    "junit-console": parse_junit_console,
}


def parse_regex(lines: list[str], test_regex: str, total_regex: str) -> Found:
    """The user's ``test_regex`` (group ``test``) and ``total_regex`` (group
    ``total``, summed), on lines cut to :data:`REGEX_LINE_CHARS`."""
    test, total = compile_re2(test_regex, "test"), compile_re2(total_regex, "total")
    ids: set[str] = set()
    expected = None
    for line in lines:
        line = line[:REGEX_LINE_CHARS]
        m = total.search(line)
        if m:
            try:
                expected = (expected or 0) + int(m.group("total"))
            except (TypeError, ValueError):
                return unknown("regex", "regex: total_regex's group is not a number")
            continue
        m = test.search(line)
        if m and m.group("test"):
            ids.add(_norm(m.group("test")))
    return _counted("regex", ids, expected)


# Where a failed log's middle was dropped (actions.FailedLogReader): a
# parser that carries context across lines starts afresh after it.
CUT = "\x00(cut)"
# Parsers whose totals are one line at the end of the run: a log whose
# middle was dropped can still be checked against them. The others add
# totals up over the log (cargo per binary, Maven per module, a regex) or
# print none (go): a dropped middle could hide a failure and its count.
TAIL_TOTALS = frozenset({"pytest", "jest", "vitest"})


def parse_log(parser: str, lines: list[str], check: CheckConfig | None = None, *,
              cut: int = 0, cut_at: int | None = None) -> Found:
    """``lines`` (a failed log's kept head and tail; ``cut`` lines dropped
    at ``cut_at``) read by ``parser``."""
    if cut:
        if parser not in TAIL_TOTALS:
            return unknown(parser, f"{parser}: the log's middle ({_plural(cut, 'line')}) was "
                                   f"not read, and {parser}'s totals cannot be checked without it")
        at = len(lines) if cut_at is None else cut_at
        lines = [*lines[:at], CUT, *lines[at:]]
    if parser == "regex":
        if check is None or not check.test_regex or not check.total_regex:
            return unknown("regex", "regex: no test_regex / total_regex")
        return parse_regex(lines, check.test_regex, check.total_regex)
    return PARSERS[parser](lines)


def _junit_cases(data: str | bytes) -> list[tuple[str, int]]:
    """The failing ``<testcase>``s of one JUnit XML file: ``(<classname or
    file>::<name>, its <failure> and <error> elements)``. No DTD and no
    entities are accepted. Raises ValueError."""
    raw = data.encode() if isinstance(data, str) else data
    if b"<!DOCTYPE" in raw[:4096] or b"<!ENTITY" in raw:
        raise ValueError("a DTD or an entity (refused)")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not valid XML: {e}") from None
    out = []
    cases = 0
    for case in root.iter("testcase"):
        cases += 1
        if cases > JUNIT_MAX_CASES:
            raise ValueError(f"more than {JUNIT_MAX_CASES} test cases")
        bad = len(case.findall("failure")) + len(case.findall("error"))
        if not bad:
            continue
        suite = case.get("classname") or case.get("file") or ""
        name = _norm(case.get("name") or "")
        out.append((f"{suite}::{name}" if suite else name, bad))
    return out


def parse_junit_xml(text: str | bytes) -> list[str]:
    """The failing ``<testcase>``s of one JUnit XML file (``<failure>`` or
    ``<error>`` inside), as ``<classname or file>::<name>``. No DTD and no
    entities are accepted. Raises ValueError."""
    return [test for test, _ in _junit_cases(text)]


_SUITE_TAG = re.compile(rb"<testsuites?[\s/>][^>]*")
_SUITE_COUNT = re.compile(rb"""\s(tests|failures|errors)\s*=\s*["'](\d+)["']""")


def suite_counts(data: bytes) -> tuple[int, int] | None:
    """``(tests, failures + errors)`` from a JUnit file's ``<testsuites>`` /
    ``<testsuite>`` start tags, read with a regex (no parse): the root
    ``<testsuites>``'s own when it has them, else the sum of its
    ``<testsuite>``s'. None when there is no such tag, or one lacks
    ``failures``."""
    tags = _SUITE_TAG.findall(data)
    if not tags:
        return None
    if tags[0].startswith(b"<testsuites"):
        root = dict(_SUITE_COUNT.findall(tags[0]))
        if b"failures" in root:
            return int(root.get(b"tests", 0)), int(root[b"failures"]) + int(root.get(b"errors", 0))
    tests = bad = 0
    suites = [t for t in tags if not t.startswith(b"<testsuites")]
    if not suites:
        return None
    for tag in suites:
        counts = dict(_SUITE_COUNT.findall(tag))
        if b"failures" not in counts:
            return None
        tests += int(counts.get(b"tests", 0))
        bad += int(counts[b"failures"]) + int(counts.get(b"errors", 0))
    return tests, bad


def read_junit_zip(path: str, clock: Callable[[], float] = time.monotonic,
                   ) -> tuple[list[str], str | None]:
    """A JUnit artifact's zip → (failing tests, an error). Its ``*.xml``
    files are read one at a time (memory: one file); a file with no
    ``<failure`` and no ``<error`` is only scanned for its testsuite counts,
    the others are parsed. The count check: the failures and errors parsed
    must be the files' own counts (when every file has them). Byte and time
    caps instead of a file count: :data:`JUNIT_MAX_XML_BYTES` scanned,
    :data:`JUNIT_MAX_FILE_BYTES` a file, :data:`JUNIT_MAX_PARSE_BYTES`
    parsed, :data:`JUNIT_MAX_SECONDS`."""
    deadline = clock() + JUNIT_MAX_SECONDS
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        return [], f"not a zip: {e}"
    with z:
        infos = [i for i in z.infolist() if not i.is_dir() and i.filename.endswith(".xml")]
        if not infos:
            return [], "no XML file in it"
        if sum(i.file_size for i in infos) > JUNIT_MAX_XML_BYTES:
            return [], f"its XML is larger than {JUNIT_MAX_XML_BYTES >> 20} MB"
        out: list[str] = []
        scanned = parsed = tests = expected = found = 0
        counted = True
        for info in sorted(infos, key=lambda i: i.filename):
            if clock() > deadline:
                return [], f"reading its {len(infos)} XML files took over {JUNIT_MAX_SECONDS:g} s"
            name = info.filename.rsplit("/", 1)[-1]
            if info.file_size > JUNIT_MAX_FILE_BYTES:
                return [], f"{name} is larger than {JUNIT_MAX_FILE_BYTES >> 20} MB"
            try:
                with z.open(info) as f:
                    data = f.read(JUNIT_MAX_FILE_BYTES + 1)
            except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
                return [], f"{name}: {e}"
            if len(data) > JUNIT_MAX_FILE_BYTES:
                return [], f"{name} is larger than {JUNIT_MAX_FILE_BYTES >> 20} MB"
            scanned += len(data)
            if scanned > JUNIT_MAX_XML_BYTES:
                return [], f"its XML is larger than {JUNIT_MAX_XML_BYTES >> 20} MB"
            counts = suite_counts(data)
            if counts is None:
                counted = False
            else:
                tests += counts[0]
                expected += counts[1]
            if b"<failure" not in data and b"<error" not in data:
                continue
            parsed += len(data)
            if parsed > JUNIT_MAX_PARSE_BYTES:
                return [], f"its failing XML is larger than {JUNIT_MAX_PARSE_BYTES >> 20} MB"
            try:
                cases = _junit_cases(data)
            except ValueError as e:
                return [], f"{name}: {e}"
            out += [test for test, _ in cases]
            found += sum(n for _, n in cases)
            if len(out) > JUNIT_MAX_CASES:
                return [], f"more than {JUNIT_MAX_CASES} failing test cases"
        if counted and found != expected:
            return [], (f"found {found} of {expected} failures and errors its "
                        f"{_plural(len(infos), 'file')} count ({tests} tests)")
        return out, None


def pick_artifacts(artifacts: list[dict[str, Any]], glob: str, job: str) -> tuple[list[dict[str, Any]], str]:
    """The artifacts of a run that hold ``job``'s JUnit XML: those whose
    name matches ``glob``; for a matrix job with several, those that name
    every matrix value. Returns (artifacts, why none)."""
    matched = [a for a in artifacts if fnmatch.fnmatchcase(str(a.get("name", "")), glob)
               and not a.get("expired")]
    if not matched:
        expired = any(fnmatch.fnmatchcase(str(a.get("name", "")), glob) for a in artifacts)
        return [], (f"the JUnit artifact {glob} has expired" if expired
                    else f"no JUnit artifact {glob}")
    values = matrix_values(job)
    if len(matched) > 1 and values:
        named = [a for a in matched if all(v in str(a.get("name", "")) for v in values)]
        if not named:
            return [], (f"{len(matched)} artifacts match {glob} and none names "
                        f"{', '.join(values)}")
        matched = named
    big = [a for a in matched if int(a.get("size_in_bytes") or 0) > JUNIT_MAX_BYTES]
    if big:
        return [], f"the JUnit artifact {big[0].get('name')} is larger than 20 MB"
    return matched, ""


# ----------------------------------------------------------------------
# One check's verdict
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """One failing check on the head, compared with the base."""

    check: str
    job: str
    workflow: str
    run_id: int
    unrelated: bool
    # The branch's and the base's failing tests (None: unknown); the
    # branch's that are not on the base (when both are known).
    tests: frozenset[str] | None = None
    base_tests: frozenset[str] | None = None
    new: frozenset[str] = frozenset()
    # The comparison point and what it was compared with ("main at
    # 3f2a9c1", "origin/main at 3f2a9c1 (project x)").
    point: str | None = None
    compared: str = ""
    # What decided it, in a few words ("both failing tests also fail on
    # main at 3f2a9c1", "no JUnit artifact junit-*").
    detail: str = ""
    # Sync: the base's newest run of that workflow no longer fails these
    # tests (None: not known yet).
    base_fixed: bool | None = None
    fixed_detail: str = ""
    # Some data is still to be read (not a failure to read it).
    pending: bool = False
    # An aggregate job: the failing jobs of its run it waits on.
    aggregate_of: tuple[str, ...] = ()
    # Mechanism B (engine/attest.py): "B" when an accepted attestation
    # explains the check; the attestation behind it, or the one waiting
    # for the user's confirmation (attest_pending); why the latest one no
    # longer counts ("attestation rejected", "head moved, attestation
    # expired").
    mechanism: str = "A"
    attestation: Mapping[str, Any] | None = None
    attest_pending: bool = False
    attest_note: str = ""

    @property
    def name(self) -> str:
        return bare_job(self.job)

    @property
    def known_related(self) -> bool:
        """Related on known data (a new failing test), not for want of it."""
        return not self.unrelated and bool(self.new)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "job": self.job,
            "workflow": self.workflow,
            "run_id": self.run_id,
            "mechanism": self.mechanism,
            "unrelated": self.unrelated,
            "tests": None if self.tests is None else sorted(self.tests),
            "base_tests": None if self.base_tests is None else sorted(self.base_tests),
            "new": sorted(self.new),
            "point": self.point,
            "compared": self.compared,
            "detail": self.detail,
            "base_fixed": self.base_fixed,
            "fixed_detail": self.fixed_detail,
        } | ({"aggregate_of": list(self.aggregate_of)} if self.aggregate_of else {}) \
          | ({"attestation": dict(self.attestation), "attest_pending": self.attest_pending}
             if self.attestation is not None else {}) \
          | ({"attest_note": self.attest_note} if self.attest_note else {})


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


@dataclass(frozen=True)
class CiTests:
    """A work item's failing checks on its head under mechanism A."""

    # The failing Actions jobs, each compared.
    checks: tuple[CheckResult, ...] = ()
    # Failing checks with no test data at all: not Actions runs (a commit
    # status, another provider) or red runs whose jobs are not read yet.
    others: tuple[tuple[str, str], ...] = ()
    # R4c: the failing checks whose table makes them ``nongating`` or
    # ``policy`` (a check id, or an other's lower-case name); the rest gate.
    gates: Mapping[str, str] = field(default_factory=dict)
    # R5a: the base's required checks (engine/required_checks.py), and
    # where the gating comes from: ``protection``, ``default_branch``
    # (the base requires nothing; the default branch's set), ``config``
    # ([ci.check] gating / policy alone) or ``none``.
    required: Required | None = None
    required_source: str = "none"

    @property
    def red(self) -> bool:
        return bool(self.checks or self.others)

    def gate(self, c: CheckResult | str) -> str:
        """A related check's effective gate: a non-gating check whose
        failing tests are known to be the branch's (they pass at the base
        point) gates again; with unknown test data it stays non-gating."""
        if isinstance(c, str):
            return self.gates.get(c, "gating")
        gate = self.gates.get(c.check, "gating")
        return "gating" if gate == "nongating" and c.known_related else gate

    def _related_of(self, gate: str) -> list[str]:
        return [c.check for c in self.checks if not c.unrelated and self.gate(c) == gate] \
            + [o for o, _ in self.others if self.gate(o) == gate]

    @property
    def gating_failures(self) -> list[str]:
        """The related checks that gate the merge (R4c)."""
        return self._related_of("gating")

    @property
    def policy_failures(self) -> list[str]:
        return self._related_of("policy")

    @property
    def nongating_failures(self) -> list[str]:
        return self._related_of("nongating")

    @property
    def only_nongating(self) -> bool:
        """Red, and every failing check is non-gating, a policy check or
        explained by the base: nothing the branch must fix before merging."""
        return self.red and not self.gating_failures \
            and bool(self.policy_failures or self.nongating_failures)

    def _names(self, checks: list[str]) -> str:
        names = {c.check: c.name for c in self.checks} | dict(self.others)
        shown = [names.get(c, c) for c in checks]
        return ", ".join(shown[:2]) + (f" +{len(shown) - 2}" if len(shown) > 2 else "")

    def first_policy(self) -> str | None:
        policy = self.policy_failures
        return self._names(policy[:1]) if policy else None

    @property
    def unrelated_checks(self) -> frozenset[str]:
        return frozenset(c.check for c in self.checks if c.unrelated)

    @property
    def attested(self) -> frozenset[str]:
        """The checks an accepted attestation explains (mechanism B)."""
        return frozenset(c.check for c in self.checks if c.unrelated and c.mechanism == "B")

    @property
    def attestation_pending(self) -> frozenset[str]:
        """The checks whose attestation waits for the user's confirmation."""
        return frozenset(c.check for c in self.checks if c.attest_pending)

    @property
    def related(self) -> list[str]:
        return [c.check for c in self.checks if not c.unrelated] + [o for o, _ in self.others]

    @property
    def unrelated(self) -> bool | None:
        """Every failing check explained: True; one with a new failing test:
        False; else (some data unknown) unknown."""
        if not self.red:
            return False
        if any(c.known_related for c in self.checks):
            return False
        if self.others or not all(c.unrelated for c in self.checks):
            return None
        return True

    @property
    def summary(self) -> str:
        """``base also red: 3 tests``, ``eval-suite attested by agent a2``
        (empty: nothing explained)."""
        tests = set()
        for c in self.checks:
            if c.unrelated and c.mechanism == "A" and c.tests:
                tests |= c.tests
        parts = [f"base also red: {_plural(len(tests), 'test')}"] if tests else []
        parts += [f"{c.name} attested by {(c.attestation or {}).get('actor_text', 'an agent')}"
                  for c in self.checks if c.unrelated and c.mechanism == "B"]
        return "; ".join(parts)

    def settled(self) -> str:
        """What explains the red when every failing check is unrelated or
        waits for the user to confirm an attestation (``base also red: 2
        tests; eval-suite attested``); empty otherwise. A delegated Fix CI
        that cleared this way did not leave CI red to the user."""
        if self.unrelated is True:
            return self.summary
        if self.others or not self.checks \
                or not all(c.unrelated or c.attest_pending for c in self.checks):
            return ""
        pending = sorted({c.name for c in self.checks if c.attest_pending})
        return "; ".join([x for x in (self.summary,) if x] + [f"{n} attested" for n in pending])

    @property
    def note(self) -> str | None:
        """The row's ``ci_note`` chip: only when every failing check is
        explained, or none gates the merge (``eval-suite red · not
        gating``)."""
        if self.unrelated is True:
            return f"CI red · unrelated ({self.summary})"
        if not self.only_nongating:
            return None
        parts = [f"{self._names(self.nongating_failures)} red · not gating"] \
            if self.nongating_failures else []
        if self.policy_failures:
            parts.append(f"{self._names(self.policy_failures)} red · policy")
        return "; ".join(parts)

    @property
    def base_fixed(self) -> frozenset[str] | None:
        if self.unrelated is not True:
            return frozenset()
        if any(c.base_fixed is None for c in self.checks):
            return None
        return frozenset(c.check for c in self.checks if c.base_fixed)

    def first_related(self) -> str | None:
        """The first related check's name, with its new tests' count when
        known: ``unit-tests (2 new)``; a gating one first."""
        gating = set(self.gating_failures)
        if gating and not any(c.check in gating for c in self.checks):
            return next(n for o, n in self.others if o in gating)
        for c in self.checks:
            if not c.unrelated and (not gating or c.check in gating):
                # Short: the row's state has five words.
                if c.attest_note:
                    return f"{c.name} ({c.attest_note})"
                return f"{bare_job(c.job)} ({len(c.new)} new)" if c.new else bare_job(c.job)
        return self.others[0][1] if self.others else None

    def first_pending(self) -> str | None:
        return next((c.name for c in self.checks if c.attest_pending), None)

    def first_fixed(self) -> str | None:
        return next((bare_job(c.job) for c in self.checks if c.base_fixed), None)

    def related_detail(self) -> str:
        """The item's detail: each related check, its new tests named."""
        out = []
        for c in self.checks:
            if c.unrelated:
                continue
            if c.new:
                shown = sorted(c.new)
                more = f" (+{len(shown) - 5} more)" if len(shown) > 5 else ""
                out.append(f"{c.job}: {_plural(len(c.new), 'new failing test')}: "
                           + ", ".join(t.split("::", 1)[-1] for t in shown[:5]) + more)
            else:
                out.append(f"{c.job}: {c.detail}")
        out += [f"{name}: no test data" for _, name in self.others]
        return "; ".join(out)

    def fixed_detail(self) -> str:
        return "; ".join(f"{c.job}: {c.fixed_detail}" for c in self.checks if c.base_fixed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unrelated": self.unrelated,
            "summary": self.summary,
            "checks": [c.to_dict() | ({"gate": self.gate(c)} if self.gates else {})
                       for c in self.checks],
            "others": [{"check": o, "name": n, "unrelated": False,
                        "detail": "no test data: not an Actions job, or its jobs are not read yet"}
                       | ({"gate": self.gate(o)} if self.gates else {})
                       for o, n in self.others],
        } | ({"only_nongating": self.only_nongating} if self.gates else {}) \
          | ({"required": self.required_dict()} if self.required_source != "none" else {})

    def required_dict(self) -> dict[str, Any]:
        """Which checks gate and where that came from (rules_explain)."""
        out: dict[str, Any] = {"source": self.required_source}
        if self.required is not None:
            out |= self.required.to_dict()
        return out


# ----------------------------------------------------------------------
# The engine side: fetching and the per-pass verdict
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """What the status projection knows about a branch: its open PR's
    number and base branch (else the default branch), its I6 project."""

    branch: str
    base: str
    pr_number: int | None = None
    project: str | None = None


def _git_runner(repo_root: str) -> Git:
    from lazyagent.worktree_manager import _run

    def git(args: list[str]) -> tuple[int, str]:
        try:
            proc = _run(["git", *args], capture_output=True, text=True, cwd=repo_root or None,
                        timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return 1, ""
        return proc.returncode, proc.stdout

    return git


def _short(sha: str | None) -> str:
    return (sha or "")[:7]


def _ref_branch(ref: str) -> str:
    for prefix in ("refs/remotes/origin/", "origin/", "refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def _put(cache: OrderedDict, key: Any, value: Any) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > CACHE:
        cache.popitem(last=False)


class _Budget(Exception):
    """This poll's test-data reads are spent."""


class CiTestsHub:
    """Mechanism A's data for one repo, beside the Actions hub that owns it
    (the same ``gh`` runner, caps and back-off)."""

    def __init__(self, hub: ActionsHub, *, git: Git | None = None,
                 state_path: Path | None = None) -> None:
        self.hub = hub
        self.git = git or _git_runner(hub.repo_root)
        self.state_path = state_path
        self.config: CiConfig = NO_CONFIG
        self.targets: dict[str, Target] = {}
        # (run id, attempt, job) → Found
        self._found: OrderedDict[tuple[int, int, str], Found] = OrderedDict()
        # (run id, attempt) → its artifacts list
        self._artifacts: OrderedDict[tuple[int, int], list[dict[str, Any]]] = OrderedDict()
        # artifact id → failing tests (or an error)
        self._junit: OrderedDict[int, tuple[list[str], str | None]] = OrderedDict()
        # run id → (the merge ref's base parent, why unknown): recorded once
        self._merge_ref: OrderedDict[int, tuple[str | None, str]] = OrderedDict()
        # (a, b) → (the one merge-base, why unknown)
        self._merge_base: OrderedDict[tuple[str, str], tuple[str | None, str]] = OrderedDict()
        # (run id, attempt) → the names of its failed jobs (base runs)
        self._failed: OrderedDict[tuple[int, int], frozenset[str]] = OrderedDict()
        # sha → that commit's own runs (actions/runs?head_sha=), read once
        # while none of them is still running
        self._point_runs: OrderedDict[str, list[WorkflowRun]] = OrderedDict()
        self.version = 0
        self._budget = 0
        # (run id, attempt, job) → failed reads so far
        self._tries: dict[tuple[int, int, str], int] = {}
        self._runs_seen: list[WorkflowRun] | None = None
        self._memo: dict[Any, CiTests | None] = {}
        self._loaded = False
        # Mechanism B (engine/attest.py): the engine's attestations, applied
        # on top of A's verdict.
        self.attestations: Any = None

    # --- State ---

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.state_path is None:
            return
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        try:
            for key, value in data.get("found", {}).items():
                run_id, attempt, job = key.split(":", 2)
                self._found[(int(run_id), int(attempt), job)] = Found.from_json(value)
            for key, (sha, why) in data.get("merge_ref", {}).items():
                self._merge_ref[int(key)] = (sha, why)
            for key, names in data.get("failed", {}).items():
                run_id, attempt = key.split(":")
                self._failed[(int(run_id), int(attempt))] = frozenset(names)
        except (AttributeError, TypeError, ValueError):
            self._found.clear()
            self._merge_ref.clear()
            self._failed.clear()

    def _save(self) -> None:
        if self.state_path is None:
            return
        data = {
            "version": 1,
            "found": {f"{r}:{a}:{j}": f.to_json() for (r, a, j), f in self._found.items()},
            "merge_ref": {str(k): list(v) for k, v in self._merge_ref.items()},
            "failed": {f"{r}:{a}": sorted(v) for (r, a), v in self._failed.items()},
        }
        try:
            from lazyagent.engine.notes import _atomic_write

            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.state_path, json.dumps(data, sort_keys=True).encode())
        except OSError as e:
            log.warning("ci_tests: cannot save %s: %s", self.state_path, e)

    def _changed(self) -> None:
        self.version += 1
        self._memo.clear()

    # --- Calls ---

    def _spend(self) -> None:
        if self._budget <= 0 or not self.hub.may_call():
            raise _Budget
        self._budget -= 1

    async def _api(self, path: str) -> str:
        self._spend()
        _, _, body = await self.hub._api(path)
        return body

    # --- The poll's part ---

    async def fetch(self, verdicts: Mapping[str, CiVerdict]) -> bool:
        """Read what the red heads' comparisons still lack, within this
        poll's budget; True when a verdict may have changed."""
        if not self.config.configured:
            return False
        self._load()
        before = self.version
        self._budget = MAX_FETCHES
        base_budget = self.config.base_fetches_per_poll
        try:
            for branch, v in sorted(verdicts.items()):
                target = self.targets.get(branch)
                if v.state != "red" or target is None:
                    continue
                for run in self._attested_runs(branch, v):
                    try:
                        await self._point_now(run, target)
                    except (TrackerError, OSError, ValueError) as e:
                        log.info("ci_tests: %s", e)
                for run, job, check in self._failing(v):
                    try:
                        await self._branch_tests(run, job, check)
                        found = self._found.get((run.id, run.attempt, job))
                        if found is None or not found.known:
                            continue
                        await self._base_tests(run, job, check, target)
                    except (TrackerError, OSError, ValueError) as e:
                        # The next job, branch or poll goes on.
                        log.info("ci_tests: %s", e)
            # Sync: the base's newest red runs, for rows all explained.
            for branch, v in sorted(verdicts.items()):
                target = self.targets.get(branch)
                if v.state != "red" or target is None:
                    continue
                result = self._analyze(branch, v, target)
                if result is None or result.unrelated is not True:
                    continue
                for c in result.checks:
                    newest = self._newest_base_run(c, v, target)
                    if newest is None or not newest.red or base_budget <= 0:
                        continue
                    job_cfg = self.config.tests_for(c.check, c.job)
                    if (newest.id, newest.attempt, c.job) in self._found or job_cfg is None:
                        continue
                    base_budget -= 1
                    failed = await self._jobs(newest)
                    if failed is not None and c.job in failed:
                        await self._run_tests(newest, c.job, job_cfg, self._source_of(c))
        except _Budget:
            pass
        except (TrackerError, OSError, ValueError) as e:
            log.info("ci_tests: %s", e)
        if self.version != before:
            self._save()
            return True
        return False

    async def _jobs(self, run: WorkflowRun) -> frozenset[str] | None:
        """A base run's failed jobs' names (kept here too: the hub's cache
        is small, and a verdict must not flip when it drops one)."""
        names = self._failed_of(run)
        if names is not None:
            return names
        self._spend()
        jobs = await self.hub.jobs(run)
        if jobs is None:
            return None
        names = frozenset(j.name for j in jobs)
        _put(self._failed, (run.id, run.attempt), names)
        self._changed()
        return names

    def _attested_runs(self, branch: str, v: CiVerdict) -> list[WorkflowRun]:
        """The red runs on the head whose failing jobs an attestation covers:
        their base point is read, so a moved point expires it."""
        attest = self.attestations
        if attest is None:
            return []
        covered = attest.covered(branch)
        return [r for r in v.red
                if any(check_id(r, j.name) in covered for j in r.failed_jobs or ())]

    async def _point_now(self, run: WorkflowRun, target: Target) -> tuple[str | None, str, str]:
        """``(P, compared, why unknown)`` for ``run``, reading the build
        base (and the compare_to merge-base) when it is not cached yet."""
        build, why = await self._build_base(run, target)
        if build is None:
            return None, "", why
        ref, _ = self.config.compare_for(target.project)
        if ref != BUILD_BASE:
            point, why = await self._merge_base_of(build, ref)
            if point is None:
                return None, "", why
        point, _, compared, why = self._point_of(run, target)
        return point, compared, why

    async def point_for(self, run: WorkflowRun, target: Target) -> tuple[str | None, str, str]:
        """:meth:`_point_now` outside a poll (an attestation being filed):
        at most two calls, inside the hub's caps."""
        self._load()
        before, self._budget = self._budget, 2
        try:
            return await self._point_now(run, target)
        except _Budget:
            return None, "", "GitHub calls are spent for this minute"
        except (TrackerError, OSError, ValueError) as e:
            return None, "", str(e)
        finally:
            self._budget = before
            if self.version:
                self._save()

    def point_of(self, run: WorkflowRun, target: Target) -> str | None:
        """The base point from what is cached (None: unknown)."""
        return self._point_of(run, target)[0]

    def _failed_of(self, run: WorkflowRun) -> frozenset[str] | None:
        slot = (run.id, run.attempt)
        names = self._failed.get(slot)
        if names is None and slot in self.hub._jobs:
            names = frozenset(j.name for j in self.hub._jobs[slot])
            _put(self._failed, slot, names)
        return names

    def _source_of(self, c: CheckResult) -> str | None:
        found = next((f for (r, _, j), f in self._found.items() if r == c.run_id and j == c.job),
                     None)
        return found.source if found is not None else None

    def _failing(self, v: CiVerdict) -> Iterable[tuple[WorkflowRun, str, CheckConfig]]:
        for run in v.red:
            for job in run.failed_jobs or ():
                check = self.config.tests_for(check_id(run, job.name), job.name)
                if check is not None:
                    yield run, job.name, check

    async def _branch_tests(self, run: WorkflowRun, job: str, check: CheckConfig) -> None:
        await self._run_tests(run, job, check)

    async def _run_tests(self, run: WorkflowRun, job: str, check: CheckConfig,
                         source: str | None = None) -> Found:
        """``job``'s failing tests on ``run`` from the check's sources, the
        first known one (or ``source`` alone, to match the branch's)."""
        slot = (run.id, run.attempt, job)
        found = self._found.get(slot)
        if found is not None and (source is None or found.source == source):
            return found
        whys = []
        try:
            result = await self._read_sources(run, job, check, source, whys)
        except TrackerError as e:
            # A failed call is tried again on the next polls; after
            # RETRIES, the check stays related for that run attempt.
            tries = self._tries.get(slot, 0) + 1
            self._tries[slot] = tries
            if tries < RETRIES:
                raise
            result = unknown(source or check.tests_from[0], f"could not be read: {e}")
        _put(self._found, slot, result)
        self._tries.pop(slot, None)
        self._changed()
        return result

    async def _read_sources(self, run: WorkflowRun, job: str, check: CheckConfig,
                            source: str | None, whys: list[str]) -> Found:
        for spec in check.tests_from:
            if source is not None and spec != source:
                continue
            kind, _, arg = spec.partition(":")
            if kind == "junit":
                result = await self._junit_tests(run, job, arg)
            else:
                result = await self._log_tests(run, job, arg, check)
            if result.known:
                return Found(result.ids, spec, result.why)
            whys.append(result.why)
        return unknown(source or check.tests_from[0], "; ".join(whys) or "no source")

    async def _junit_tests(self, run: WorkflowRun, job: str, glob: str) -> Found:
        slot = (run.id, run.attempt)
        artifacts = self._artifacts.get(slot)
        if artifacts is None:
            body = await self._api(f"repos/{{owner}}/{{repo}}/actions/runs/{run.id}/artifacts"
                                   "?per_page=100")
            try:
                artifacts = [a for a in json.loads(body).get("artifacts", []) if isinstance(a, dict)]
            except (ValueError, AttributeError):
                return unknown("junit", "junit: a bad artifacts list")
            _put(self._artifacts, slot, artifacts)
        picked, why = pick_artifacts(artifacts, glob, job)
        if not picked:
            return unknown("junit", f"junit: {why}")
        ids: list[str] = []
        for a in picked:
            aid = int(a.get("id") or 0)
            got = self._junit.get(aid)
            if got is None:
                got = await self._download(aid)
                _put(self._junit, aid, got)
            tests, error = got
            if error:
                return unknown("junit", f"junit: {a.get('name')}: {error}")
            ids += tests
        n = len(set(ids))
        return _known("junit", ids, f"junit: {_plural(n, 'failing test')} in "
                                    + ", ".join(str(a.get("name")) for a in picked))

    async def _download(self, aid: int) -> tuple[list[str], str | None]:
        """The artifact's zip, streamed to a temporary file (at most
        :data:`JUNIT_MAX_BYTES`), then read by :func:`read_junit_zip`."""
        self._spend()
        with tempfile.TemporaryDirectory(prefix="lazyagent-junit-") as tmp:
            path = Path(tmp) / "artifact.zip"
            size = 0
            with open(path, "wb") as f:
                def sink(chunk: bytes) -> bool:
                    nonlocal size
                    size += len(chunk)
                    if size > JUNIT_MAX_BYTES:
                        return False
                    f.write(chunk)
                    return True

                code, err = await self.hub._stream(
                    ["api", f"repos/{{owner}}/{{repo}}/actions/artifacts/{aid}/zip"], sink,
                    JUNIT_TIMEOUT)
            if size > JUNIT_MAX_BYTES:
                return [], f"larger than {JUNIT_MAX_BYTES >> 20} MB"
            if code != 0:
                raise TrackerError(f"gh api artifacts/{aid}/zip: {err.strip() or code}")
            return await asyncio.to_thread(read_junit_zip, str(path))

    async def _log_tests(self, run: WorkflowRun, job: str, parser: str,
                         check: CheckConfig) -> Found:
        logs = self.hub._logs.get((run.id, run.attempt))
        if logs is None:
            self._spend()
            await self.hub.summary([run])
            logs = self.hub._logs.get((run.id, run.attempt))
            if logs is None:
                raise TrackerError(f"the failed log of run {run.id} could not be read")
        entry = next((j for j in logs if j["name"] == job), None)
        if entry is None:
            return unknown(parser, f"{parser}: no failed log for {job}")
        try:
            return parse_log(parser, entry["lines"], check, cut=entry.get("cut", 0),
                             cut_at=entry.get("cut_at"))
        except ValueError as e:
            return unknown(parser, f"{parser}: {e}")

    # --- The base point ---

    async def _build_base(self, run: WorkflowRun, target: Target) -> tuple[str | None, str]:
        if run.event in ("pull_request",):
            got = self._merge_ref.get(run.id)
            if got is None:
                if target.pr_number is None:
                    got = (None, "a pull_request run with no open PR")
                else:
                    body = await self._api(
                        f"repos/{{owner}}/{{repo}}/commits/pull/{target.pr_number}/merge")
                    got = merge_ref_base(body, run.head_sha)
                _put(self._merge_ref, run.id, got)
                self._changed()
            return got
        if run.event in ("push", "workflow_dispatch"):
            return await self._merge_base_of(run.head_sha, f"origin/{target.base}")
        return None, f"a {run.event or 'run of unknown'} event: its build base is not known"

    async def _merge_base_of(self, a: str, b: str) -> tuple[str | None, str]:
        key = (a, b)
        got = self._merge_base.get(key)
        if got is None:
            code, out = await asyncio.to_thread(self.git, ["merge-base", "--all", a, b])
            bases = out.split()
            if code != 0 or not bases:
                got = (None, f"git merge-base {_short(a)} {b} failed")
            elif len(bases) > 1:
                got = (None, f"{_short(a)} and {b} have {len(bases)} merge-bases")
            else:
                got = (bases[0], "")
            _put(self._merge_base, key, got)
            self._changed()
        return got

    def _point_of(self, run: WorkflowRun, target: Target) -> tuple[str | None, str, str, str]:
        """``(P, base branch, compared, why unknown)`` from what is cached."""
        build = self._cached_build_base(run, target)
        if build[0] is None:
            return None, target.base, "", build[1] or "the build base is not read yet"
        ref, where = self.config.compare_for(target.project)
        if ref == BUILD_BASE:
            return build[0], target.base, f"{target.base} at {_short(build[0])}", ""
        got = self._merge_base.get((build[0], ref))
        suffix = f" ({where})" if where else ""
        if got is None:
            return None, _ref_branch(ref), "", f"{ref}: not read yet"
        if got[0] is None:
            return None, _ref_branch(ref), "", got[1]
        return got[0], _ref_branch(ref), f"{ref} at {_short(got[0])}{suffix}", ""

    def _cached_build_base(self, run: WorkflowRun, target: Target) -> tuple[str | None, str]:
        if run.event == "pull_request":
            return self._merge_ref.get(run.id) or (None, "")
        if run.event in ("push", "workflow_dispatch"):
            return self._merge_base.get((run.head_sha, f"origin/{target.base}")) or (None, "")
        return None, f"a {run.event or 'run of unknown'} event: its build base is not known"

    async def _base_tests(self, run: WorkflowRun, job: str, check: CheckConfig,
                          target: Target) -> None:
        build, _ = await self._build_base(run, target)
        if build is None:
            return
        ref, _ = self.config.compare_for(target.project)
        if ref != BUILD_BASE:
            point, _ = await self._merge_base_of(build, ref)
            if point is None:
                return
        point, base_branch, _, _ = self._point_of(run, target)
        if point is None:
            return
        base_run = self._base_run(run, point, base_branch)
        if base_run is None and (point not in self._point_runs
                                 or any(r.status != "completed" for r in self._point_runs[point])):
            body = await self._api(f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={point}"
                                   "&per_page=100")
            from lazyagent.engine.actions import parse_runs

            runs = parse_runs(body)
            if self._point_runs.get(point) != runs:
                _put(self._point_runs, point, runs)
                self._changed()
            base_run = self._base_run(run, point, base_branch)
        if base_run is None:
            return
        kept = self._point_runs.get(point, [])
        if base_run not in kept:
            # The window moves on; the run at P must not leave with it.
            _put(self._point_runs, point, [r for r in kept if r.id != base_run.id] + [base_run])
        if not base_run.red:
            return
        failed = await self._jobs(base_run)
        if failed is None or job not in failed:
            return
        branch_found = self._found.get((run.id, run.attempt, job))
        await self._run_tests(base_run, job, check,
                              branch_found.source if branch_found is not None else None)

    def _base_run(self, run: WorkflowRun, point: str, base_branch: str) -> WorkflowRun | None:
        """The base branch's own run of ``run``'s workflow at exactly
        ``point`` (the newest attempt), from the poll's runs or the point's
        own read."""
        by_id = {r.id: r for r in self._point_runs.get(point, [])}
        by_id.update((r.id, r) for r in self.hub.all_runs() if r.head_sha == point)
        same = [r for r in by_id.values() if r.head_sha == point and r.head_branch == base_branch
                and r.event in ("push", "") and _same_workflow(r, run)]
        if not same:
            return None
        return max(same, key=lambda r: (r.created_at, r.id, r.attempt))

    def _newest_base_run(self, c: CheckResult, v: CiVerdict, target: Target) -> WorkflowRun | None:
        """The base branch's newest run of the check's workflow after the
        one at the comparison point (from the poll's runs only)."""
        run = next((r for r in v.red if r.id == c.run_id), None)
        if run is None or c.point is None:
            return None
        _, base_branch, _, _ = self._point_of(run, target)
        at = self._base_run(run, c.point, base_branch)
        if at is None:
            return None
        newer = [r for r in self.hub.runs if r.head_branch == base_branch and _same_workflow(r, run)
                 and r.event in ("push", "") and (r.created_at, r.id) > (at.created_at, at.id)]
        if not newer:
            return None
        return max(newer, key=lambda r: (r.created_at, r.id))

    # --- What a pass reads ---

    def analysis(self, branch: str | None, v: CiVerdict | None, pr: PrInfo | None,
                 target: Target | None = None) -> CiTests | None:
        """The work item's mechanism-A verdict; None when no check has
        ``tests_from`` (nothing configured: the rows are as before)."""
        if not self.active or not branch:
            return None
        if self.hub.runs is not self._runs_seen:
            # A new runs window: the base's newest runs may have changed.
            self._runs_seen = self.hub.runs
            self._memo.clear()
        target = target or self.targets.get(branch)
        attest = self.attestations
        key = (branch, v, None if pr is None else (pr.head_sha, pr.state, tuple(
            (c.name, c.conclusion, c.details_url) for c in pr.checks)), target, self.version,
            None if attest is None else attest.version)
        if key in self._memo:
            return self._memo[key]
        self._load()
        result = self._analyze(branch, v, target, pr)
        if result is not None and attest is not None and v is not None:
            result = attest.apply(branch, v.head, result, self.config)
        if len(self._memo) > 256:
            self._memo.clear()
        self._memo[key] = result
        return result

    def _analyze(self, branch: str, v: CiVerdict | None, target: Target | None,
                 pr: PrInfo | None = None) -> CiTests | None:
        from lazyagent.engine.actions import other_checks

        others: list[tuple[str, str]] = []
        if pr is not None and pr.state.upper() == "OPEN":
            others += [(c["name"].lower(), c["name"]) for c in other_checks(pr)]
            if v is None or v.state != "red":
                # An Actions red the verdict does not see (no runs read).
                if v is None:
                    others += [(c.name.lower(), c.name) for c in pr.checks
                               if c.conclusion.upper() in ("FAILURE", "ERROR", "TIMED_OUT")
                               and "/actions/runs/" in c.details_url]
                return self._gated((), others, target, pr)
        if v is None or v.state != "red":
            return self._gated((), others, target, pr)
        checks: list[CheckResult] = []
        for run in v.red:
            if not run.failed_jobs:
                name = run.path or run.workflow
                others.append((name.lower(), run.workflow))
                continue
            aggregates = {j.name for j in run.failed_jobs if self._is_aggregate(run, j)}
            own = {j.name: self._check(run, j.name, target, v) for j in run.failed_jobs
                   if j.name not in aggregates}
            for job in run.failed_jobs:
                checks.append(own[job.name] if job.name in own
                              else _aggregate_result(run, job.name, list(own.values())))
        return self._gated(checks, others, target, pr)

    def _gated(self, checks: Iterable[CheckResult], others: Iterable[tuple[str, str]],
               target: Target | None = None, pr: PrInfo | None = None) -> CiTests:
        """The verdict with each failing check's gate (R4c). A [ci.check]
        that says gating or policy wins; else, with the base's required
        checks known (R5a), a red check that is not required gates only
        until every required check has passed on the head."""
        checks, others = tuple(checks), tuple(others)
        required = self.required_for(target)
        passed = required is not None and required.all_passed(pr)

        def gate(names: tuple[str, ...], check: str, job: str) -> str:
            own = self.config.own_gate(check, job)
            if own is not None:
                return own
            if passed and required is not None and not required.covers(*names):
                return "nongating"
            return self.config.gate_of(check, job)

        gates = {c.check: g for c in checks
                 if (g := gate((c.job, f"{c.workflow} / {c.job}", c.check), c.check, c.job))
                 != "gating"}
        gates |= {o: g for o, n in others if (g := gate((n,), n, n)) != "gating"}
        source = required.source if required is not None \
            else ("config" if self.config.gates_set else "none")
        return CiTests(checks, others, gates, required, source)

    def required_for(self, target: Target | None) -> Required | None:
        """The target's base's required checks; None when unknown, off or
        nothing is required."""
        if target is None or not self.config.gating_from_protection:
            return None
        return self.hub.required.for_base(target.base)

    @property
    def active(self) -> bool:
        """A pass builds a verdict: a [ci] table says something, or a
        base's required checks are known (R5a)."""
        return self.config.configured or (
            self.config.gating_from_protection and self.hub.required.known())

    def _is_aggregate(self, run: WorkflowRun, job: WorkflowJob) -> bool:
        """``aggregate`` when the check sets it; else, only with ``[ci_unrelated]
        detect_aggregates``, a job with no ``tests_from`` that started only
        once every other failing job of its run had ended (it waits on them:
        ``needs``) and runs at most :data:`AGGREGATE_STEPS` steps of its
        own."""
        cfg = self.config.check(check_id(run, job.name), job.name)
        if cfg is not None and cfg.aggregate is not None:
            return cfg.aggregate
        if not self.config.detect_aggregates or (cfg is not None and cfg.tests_from):
            return False
        others = [j for j in run.failed_jobs or () if j.name != job.name]
        if not others or not job.started_at or not 0 < job.own_steps <= AGGREGATE_STEPS:
            return False
        return all(0 < j.completed_at <= job.started_at for j in others)

    def _check(self, run: WorkflowRun, job: str, target: Target | None, v: CiVerdict) -> CheckResult:
        cid = check_id(run, job)
        base = dict(check=cid, job=job, workflow=run.workflow, run_id=run.id)
        cfg = self.config.check(cid, job)
        if cfg is None or not cfg.tests_from:
            return CheckResult(**base, unrelated=False,
                               detail="no tests_from for this check (no test data)")
        found = self._found.get((run.id, run.attempt, job))
        if found is None:
            return CheckResult(**base, unrelated=False, pending=True,
                               detail="its failing tests are not read yet")
        if not found.known:
            return CheckResult(**base, unrelated=False, detail=found.why)
        tests = frozenset(f"{cid}::{t}" for t in found.ids or ())
        if target is None:
            return CheckResult(**base, unrelated=False, tests=tests,
                               detail="no base branch known")
        point, base_branch, compared, why = self._point_of(run, target)
        if point is None:
            return CheckResult(**base, unrelated=False, tests=tests, pending="not read yet" in why,
                               detail=f"base point unknown: {why}")
        base_run = self._base_run(run, point, base_branch)
        common = dict(tests=tests, point=point, compared=compared)
        if base_run is None:
            return CheckResult(**base, unrelated=False, **common,
                               detail=f"no {run.workflow} run on {base_branch} at {_short(point)}")
        if base_run.status != "completed":
            return CheckResult(**base, unrelated=False, **common, pending=True,
                               detail=f"the {run.workflow} run on {base_branch} at "
                                      f"{_short(point)} is still running")
        if not base_run.red:
            return CheckResult(**base, unrelated=False, **common, base_tests=frozenset(),
                               new=tests, detail=f"{job} passes on {compared}")
        failed = self._failed_of(base_run)
        if failed is None:
            return CheckResult(**base, unrelated=False, **common, pending=True,
                               detail=f"the base run {base_run.id}'s jobs are not read yet")
        if job not in failed:
            return CheckResult(**base, unrelated=False, **common, base_tests=frozenset(),
                               new=tests, detail=f"{job} did not fail on {compared}")
        base_found = self._found.get((base_run.id, base_run.attempt, job))
        if base_found is None:
            return CheckResult(**base, unrelated=False, **common, pending=True,
                               detail="the base's failing tests are not read yet")
        if not base_found.known:
            return CheckResult(**base, unrelated=False, **common,
                               detail=f"on the base: {base_found.why}")
        if base_found.source != found.source:
            return CheckResult(**base, unrelated=False, **common,
                               detail=f"branch from {found.source}, base from "
                                      f"{base_found.source}: not comparable")
        base_tests = frozenset(f"{cid}::{t}" for t in base_found.ids or ())
        new = tests - base_tests
        if new:
            return CheckResult(**base, unrelated=False, **common, base_tests=base_tests, new=new,
                               detail=f"{_plural(len(new), 'failing test')} not failing on "
                                      f"{compared}")
        fixed, fixed_detail = self._fixed(run, job, tests, target, point, base_branch, base_run)
        n = len(tests)
        both = "the failing test also fails" if n == 1 else (
            "both failing tests also fail" if n == 2 else f"all {n} failing tests also fail")
        return CheckResult(**base, unrelated=True, **common, base_tests=base_tests,
                           detail=f"{both} on {compared}", base_fixed=fixed,
                           fixed_detail=fixed_detail)

    def _fixed(self, run: WorkflowRun, job: str, tests: frozenset[str], target: Target,
               point: str, base_branch: str, at: WorkflowRun) -> tuple[bool | None, str]:
        newer = [r for r in self.hub.runs if r.head_branch == base_branch and _same_workflow(r, run)
                 and r.event in ("push", "") and (r.created_at, r.id) > (at.created_at, at.id)]
        if not newer:
            return False, ""
        newest = max(newer, key=lambda r: (r.created_at, r.id))
        where = f"{base_branch} at {_short(newest.head_sha)} (run {newest.id})"
        if newest.status != "completed":
            return False, ""
        if not newest.red:
            return True, f"passes on {where}"
        failed = self._failed_of(newest)
        if failed is not None and job not in failed:
            return True, f"passes on {where}"
        found = self._found.get((newest.id, newest.attempt, job))
        if found is None or not found.known:
            return (None, "") if found is None else (False, "")
        cid = check_id(run, job)
        still = tests & frozenset(f"{cid}::{t}" for t in found.ids or ())
        return (not still), (f"its failing tests pass on {where}" if not still else "")


def _aggregate_result(run: WorkflowRun, job: str, deps: list[CheckResult]) -> CheckResult:
    """An aggregate job: unrelated when every other failing job of its run
    is (it may wait on fewer: stricter, and the row's verdict is the same,
    since those jobs are failing checks too), else related naming the
    first that is not. No test data of its own."""
    cid = check_id(run, job)
    names = tuple(d.job for d in deps)
    base = dict(check=cid, job=job, workflow=run.workflow, run_id=run.id, aggregate_of=names,
                tests=frozenset(), base_tests=frozenset())
    if not deps:
        return CheckResult(**base, unrelated=False,
                           detail=f"an aggregate job with no other failing job in run {run.id} "
                                  "(a cancelled or skipped one?)")
    points = {d.point for d in deps}
    compared = {d.compared for d in deps}
    where = dict(point=points.pop() if len(points) == 1 else None,
                 compared=compared.pop() if len(compared) == 1 else "")
    culprit = next((d for d in deps if not d.unrelated), None)
    if culprit is not None:
        return CheckResult(**base, unrelated=False, **where,
                           pending=any(d.pending for d in deps),
                           detail=f"waits on {culprit.job}, related: {culprit.detail}")
    fixed = [d.base_fixed for d in deps]
    return CheckResult(**base, unrelated=True, **where,
                       base_fixed=None if None in fixed else all(fixed),
                       fixed_detail="every job it waits on passes on the base" if all(
                           f is True for f in fixed) else "",
                       detail=f"waits on {', '.join(names)}: "
                              + ("unrelated" if len(names) == 1 else "all unrelated"))


def _same_workflow(a: WorkflowRun, b: WorkflowRun) -> bool:
    if a.path and b.path:
        return a.path == b.path
    if a.workflow_id and b.workflow_id:
        return a.workflow_id == b.workflow_id
    return a.workflow == b.workflow


def merge_ref_base(body: str, head: str) -> tuple[str | None, str]:
    """``commits/pull/<n>/merge``: the base parent when one parent is the
    run's head (else the ref has moved on since the run)."""
    try:
        data = json.loads(body)
        parents = [p["sha"] for p in data.get("parents", [])]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None, "a bad merge ref response"
    if len(parents) != 2 or head not in parents:
        return None, "the PR's merge ref has moved on since the run"
    return next(p for p in parents if p != head), ""


def state_file(repo_root: str) -> Path:
    from lazyagent.engine.runtime import repo_digest, state_dir

    return state_dir() / "ci_tests" / f"{repo_digest(repo_root)}.json"


__all__ = [
    "BUILD_BASE", "AttestRules", "CheckConfig", "CheckResult", "CiConfig", "CiTests", "CiTestsHub", "Found",
    "NO_CONFIG", "PARSERS", "Target", "bare_job", "check_id", "compile_re2", "merge_parts",
    "parse_junit_xml", "parse_log", "parse_tables", "pick_artifacts",
]
