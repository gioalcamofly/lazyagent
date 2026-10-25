"""User rule files (proposal 0014, "Files, precedence, validation and
errors"): the global ``$XDG_CONFIG_HOME/lazyagent/rules.toml`` and a repo's
``.lazyagent/rules.toml``, parsed, validated rule by rule, and composed
with the built-in catalog.

A user rule::

    [[rule]]
    id = "merge"                  # an existing id replaces it, in place
    says = "Merge only off the release branch."
    and_when = 'item.branch != "release"'    # narrow the definition it replaces
    # or: when = '...' and a full [rule.then]
    before = "changes_requested"  # or after = "...": a non-locked id
    # disabled = true             # turn a non-locked rule off

Order (Q-R3/Q-R4): the locked prefix (code-fixed), then the repo's new
rules, the global new rules, then the built-ins; a replacement keeps the
place of what it replaces unless it says ``before``/``after``.

A rule that fails validation is skipped alone; what it replaced stays in
force. Rules are data: the conditions go through the closed expression
language (:mod:`.lang`) with its caps; nothing is evaluated as code.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from lazyagent.engine.rules.catalog import (
    ID_RE,
    INHERITABLE,
    THEN_FIELDS,
    THEN_KEYS,
    Catalog,
    CatalogError,
    Out,
    Rule,
    _check_const,
    _check_item,
    _close,
    _compile,
    _verb_action,
    _WANT,
)

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the tomli dependency on 3.10
    import tomli as tomllib

MAX_FILE_BYTES = 256 * 1024
MAX_RULES = 200
RULE_KEYS = frozenset({"id", "says", "when", "and_when", "then", "before", "after", "disabled"})
TOP_KEYS = frozenset({"version", "rule", "review"})
REVIEW_KEYS = frozenset({"not_human"})
# Tables later phases read (R4, R5): accepted, not read yet.
LATER_KEYS = frozenset({"ci", "ci_unrelated"})
SCOPES = ("global", "repo")

_HEADER = re.compile(r"^\s*\[\[\s*rule\s*\]\]")
_TABLE = re.compile(r"^\s*\[")
_TOML_LINE = re.compile(r"at line (\d+)")


@dataclass
class Entry:
    """One ``[[rule]]`` table of a file, before it is compiled."""

    raw: dict[str, Any]
    line: int | None
    # The lines of the table's keys (``when`` → 12).
    keys: dict[str, int] = field(default_factory=dict)

    @property
    def id(self) -> str:
        rid = self.raw.get("id")
        return rid if isinstance(rid, str) else ""

    def line_of(self, key: str | None) -> int | None:
        return self.keys.get(key or "", self.line)


@dataclass
class FileRules:
    """A parsed rule file: its entries, or why it did not parse."""

    scope: str
    path: str
    entries: list[Entry] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    # False: the file as a whole was ignored (TOML, size, version).
    ok: bool = True
    # [review] not_human: logins that are not people (lower case).
    not_human: list[str] = field(default_factory=list)


def problem(f: FileRules | None, line: int | None, rule: str | None, message: str,
            fallback: str) -> dict[str, Any]:
    return {
        "scope": f.scope if f is not None else None,
        "file": f.path if f is not None else None,
        "line": line,
        "rule": rule or None,
        "message": message,
        "fallback": fallback,
    }


def _lines(text: str) -> list[tuple[int, dict[str, int]]]:
    """Each ``[[rule]]`` header's line, with its keys' lines (1-based)."""
    out: list[tuple[int, dict[str, int]]] = []
    current: dict[str, int] | None = None
    for n, line in enumerate(text.splitlines(), 1):
        if _HEADER.match(line):
            current = {}
            out.append((n, current))
            continue
        if _TABLE.match(line) and not line.strip().startswith("[rule.then]"):
            current = None
            continue
        if current is not None:
            m = re.match(r"^\s*([A-Za-z_]+)\s*=\s*(.*)$", line)
            if m and m[1] not in current:
                # A multi-line string opened at the end of the line starts
                # on the next one (TOML drops that newline).
                opens = m[2].strip() in ("'''", '"""')
                current[m[1]] = n + 1 if opens else n
    return out


def parse_file(text: str, scope: str, path: str) -> FileRules:
    """``text`` as a rule file: the file-level checks (size, TOML, schema)
    and the entries with their lines. Expressions are checked when the
    file is composed."""
    f = FileRules(scope, path)
    if len(text.encode()) > MAX_FILE_BYTES:
        f.ok = False
        f.errors.append(problem(f, None, None, f"larger than {MAX_FILE_BYTES // 1024} KB",
                                "file ignored"))
        return f
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        f.ok = False
        m = _TOML_LINE.search(str(e))
        f.errors.append(problem(f, int(m[1]) if m else None, None, f"not valid TOML: {e}",
                                "file ignored"))
        return f
    if data.get("version", 1) != 1:
        f.ok = False
        f.errors.append(problem(f, 1, None, "version must be 1", "file ignored"))
        return f
    for key in sorted(set(data) - TOP_KEYS):
        if key in LATER_KEYS:
            f.warnings.append(problem(f, None, None,
                                      f"[{key}] is read by a later version; ignored for now",
                                      "ignored"))
        else:
            f.errors.append(problem(f, None, None, f"unknown top-level key {key!r}", "ignored"))
    if "review" in data:
        _review(f, data["review"], text)
    raw_rules = data.get("rule", [])
    if not isinstance(raw_rules, list):
        f.errors.append(problem(f, None, None, "rule must be an array of tables ([[rule]])",
                                "ignored"))
        return f
    lines = _lines(text)
    seen: set[str] = set()
    for i, raw in enumerate(raw_rules):
        line, keys = lines[i] if i < len(lines) else (None, {})
        if i >= MAX_RULES:
            f.errors.append(problem(f, line, None, f"more than {MAX_RULES} rules",
                                    "the rest skipped"))
            break
        if not isinstance(raw, dict):
            f.errors.append(problem(f, line, None, "a rule is a table", "skipped"))
            continue
        entry = Entry(raw, line, keys)
        rid = entry.id
        if rid and rid in seen:
            f.errors.append(problem(f, line, rid, f"the id {rid!r} is used twice in this file",
                                    "skipped"))
            continue
        seen.add(rid)
        f.entries.append(entry)
    return f


_REVIEW_HEADER = re.compile(r"^\s*\[\s*review\s*\]")


def _review(f: FileRules, table: Any, text: str) -> None:
    """``[review]``: ``not_human``, the accounts pr.human_approvals leaves
    out (a review app that signs in as a user, a CI account)."""
    line = next((n for n, s in enumerate(text.splitlines(), 1) if _REVIEW_HEADER.match(s)), None)
    if not isinstance(table, dict):
        f.errors.append(problem(f, line, None, "[review] must be a table", "ignored"))
        return
    for key in sorted(set(table) - REVIEW_KEYS):
        f.errors.append(problem(f, line, None, f"unknown key {key!r} in [review]", "ignored"))
    logins = table.get("not_human", [])
    if not isinstance(logins, list) or not all(isinstance(x, str) and x.strip() for x in logins):
        f.errors.append(problem(f, line, None, "[review] not_human must be a list of logins",
                                "ignored"))
        return
    f.not_human = [x.strip().lower() for x in logins]


# ----------------------------------------------------------------------
# Compiling one user rule
# ----------------------------------------------------------------------


class _Bad(CatalogError):
    def __init__(self, message: str, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


def _expr_key(message: str) -> str | None:
    for key in ("and_when", "when", *(f"{n}_expr" for n in THEN_FIELDS)):
        if f" {key}:" in message:
            return key
    return None


def compile_rule(entry: Entry, base: Rule | None, scope: str, path: str) -> Rule:
    """One user rule, over ``base`` (the definition it replaces, if any)."""
    raw = entry.raw
    rid = entry.id
    where = f"rule {rid or '?'}"
    if not ID_RE.match(rid):
        raise _Bad(f"{where}: id must match [a-z0-9_.-]{{1,48}}", "id")
    extra = set(raw) - RULE_KEYS
    if extra:
        raise _Bad(f"{where}: unknown key(s) {', '.join(sorted(extra))}"
                   + (" (locked and num are the built-ins' own)" if extra & {"locked", "num"}
                      else ""), sorted(extra)[0])
    says = raw.get("says", "")
    if not isinstance(says, str):
        raise _Bad(f"{where}: says is text", "says")
    and_when_src = raw.get("and_when")
    then = raw.get("then")
    if then is not None and not isinstance(then, dict):
        raise _Bad(f"{where}: then is a table", "then")
    if and_when_src is not None:
        if base is None:
            raise _Bad(f"{where}: and_when narrows an existing rule; there is no {rid!r} "
                       "to narrow (use when)", "and_when")
        if "when" in raw:
            raise _Bad(f"{where}: when or and_when, not both", "and_when")
        when = base.when
        and_when = _compile(f"{where} and_when", and_when_src, "row", "bool")
    else:
        if "when" not in raw:
            raise _Bad(f"{where}: when is required (or and_when, to narrow a rule)", None)
        when = _compile(f"{where} when", raw["when"], "row", "bool")
        and_when = None
        if then is None:
            raise _Bad(f"{where}: then is required (a table)", None)
    then = dict(then or {})
    bad = set(then) - THEN_KEYS
    if bad:
        raise _Bad(f"{where}: unknown then key(s) {', '.join(sorted(bad))}", "then")
    inherit = then.get("inherit", [])
    if not isinstance(inherit, list) or any(x not in INHERITABLE for x in inherit):
        raise _Bad(f"{where}: inherit takes {INHERITABLE}", "then")
    # A narrowed rule keeps the outputs it does not give.
    outs: dict[str, Out] = dict(base.outs) if and_when is not None and base is not None else {}
    for name in THEN_FIELDS:
        key = f"{name}_expr"
        if name in then and key in then:
            raise _Bad(f"{where}: {name} or {key}, not both", "then")
        if key in then:
            outs[name] = Out(expr=_compile(f"{where} {key}", then[key], "row", _WANT[name]))
        elif name in then:
            _check_const(where, name, then[name])
            outs[name] = Out(then[name])
    if and_when is not None and base is not None and "inherit" not in then:
        inherit = list(base.inherit)
    for name in ("ball", "state"):
        if name not in outs and name not in inherit:
            raise _Bad(f"{where}: then needs {name} (or {name}_expr, or inherit)", "then")
    ball = outs.get("ball")
    is_you = ball is not None and ball.expr is None and ball.value == "you"
    if is_you and "level" not in outs:
        outs["level"] = Out("round")
    verb, action = _verb_action(where, then)
    if and_when is not None and base is not None:
        verb = verb if "verb" in then else base.verb
        action = action if "action" in then else base.action
    if "item" in then:
        item = then["item"]
        if not isinstance(item, bool):
            raise _Bad(f"{where}: item must be true or false", "then")
    elif and_when is not None and base is not None:
        item = base.item
    else:
        # A user rule that puts the ball with you is an inbox item.
        item = is_you and not inherit
    rule = Rule(
        rid,
        base.num if base is not None else 0,
        when,
        outs,
        tuple(inherit),
        False,
        says or (base.says if base is not None and and_when is not None else ""),
        item,
        verb,
        action,
        scope,
        path,
        entry.line,
        base,
        and_when,
    )
    if item:
        _check_item(where, rule)
    return rule


# ----------------------------------------------------------------------
# Composing
# ----------------------------------------------------------------------


@dataclass
class Composed:
    catalog: Catalog
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]


def _fallback(base: Rule | None) -> str:
    if base is None:
        return "skipped"
    if base.origin == "builtin":
        return "skipped; the built-in is in force"
    return f"skipped; the {base.origin} rule is in force"


def compose(builtin: Catalog, files: list[FileRules]) -> Composed:
    """The effective catalog: ``files`` in precedence order, lowest first
    (global, then repo), over the built-ins."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for f in files:
        errors += f.errors
        warnings += f.warnings
    locked = [r for r in builtin.rules if r.locked]
    locked_ids = {r.id for r in locked}
    seq: list[Rule] = [r for r in builtin.rules if not r.locked]
    placed: list[tuple[Rule, Entry, FileRules]] = []

    known = {r.id for r in builtin.rules} | {e.id for f in files for e in f.entries}

    def index(rid: str) -> int:
        return next((i for i, r in enumerate(seq) if r.id == rid), -1)

    for f in files:
        new: list[Rule] = []
        for entry in f.entries:
            rid = entry.id
            if rid in locked_ids:
                errors.append(problem(f, entry.line, rid,
                                      f"rule {rid}: a locked rule cannot be replaced, "
                                      "narrowed or turned off", "skipped; the built-in is in force"))
                continue
            at = index(rid)
            base = seq[at] if at >= 0 else None
            disabled = entry.raw.get("disabled", False)
            if disabled is not False:
                if disabled is not True:
                    errors.append(problem(f, entry.line_of("disabled"), rid,
                                          f"rule {rid}: disabled must be true or false",
                                          _fallback(base)))
                elif set(entry.raw) - {"id", "says", "disabled"}:
                    errors.append(problem(f, entry.line_of("disabled"), rid,
                                          f"rule {rid}: a disabled rule has only id and says",
                                          _fallback(base)))
                elif base is None:
                    errors.append(problem(f, entry.line_of("disabled"), rid,
                                          f"rule {rid}: there is no rule {rid!r} to turn off",
                                          "skipped"))
                else:
                    del seq[at]
                continue
            target, key = _placement(entry)
            try:
                if key == "before and after":
                    raise _Bad(f"rule {rid}: before or after, not both", "before")
                if target is not None:
                    _check_target(rid, target, key, locked_ids, known)
                rule = compile_rule(entry, base, f.scope, f.path)
            except CatalogError as e:
                where = getattr(e, "key", None) or _expr_key(str(e))
                line, message = _at_line(entry, where, str(e))
                errors.append(problem(f, line, rid, message, _fallback(base)))
                continue
            for w in rule.when.warnings + (rule.and_when.warnings if rule.and_when else []):
                warnings.append(problem(f, entry.line, rid, f"rule {rid}: {w}", "loaded"))
            if base is not None:
                seq[at] = rule
            else:
                new.append(rule)
            if target is not None:
                placed.append((rule, entry, f))
        seq = new + seq

    # before / after, in precedence order (a repo's placement last).
    targets = {rule.id: _placement(entry)[0] for rule, entry, _ in placed}
    for rule, entry, f in placed:
        target, key = _placement(entry)
        assert target is not None
        if _cycle(rule.id, targets):
            errors.append(problem(f, entry.line_of(key), rule.id,
                                  f"rule {rule.id}: {key} = {target!r} makes a cycle of "
                                  "placements", "kept in file order"))
            continue
        if rule not in seq:
            continue
        seq.remove(rule)
        at = index(target)
        if at < 0:
            seq.insert(0, rule)
            warnings.append(problem(f, entry.line_of(key), rule.id,
                                    f"rule {rule.id}: {key} = {target!r}: that rule is not "
                                    "loaded (skipped or turned off)", "placed first"))
            continue
        seq.insert(at if key == "before" else at + 1, rule)
    rules = locked + seq
    not_human = frozenset(login for f in files for login in f.not_human)
    return Composed(Catalog(rules, list(builtin.kinds), not_human), errors, warnings)


_AT = re.compile(r" \(at (\d+)\)$")


def _at_line(entry: Entry, key: str | None, message: str) -> tuple[int | None, str]:
    """An expression error's line: the key's, plus the lines before its
    position in the expression."""
    line = entry.line_of(key)
    m = _AT.search(message)
    if m is None:
        return line, message
    source = entry.raw.get(key) if key in ("when", "and_when") else (
        (entry.raw.get("then") or {}).get(key or ""))
    if isinstance(source, str) and line is not None and key in entry.keys:
        line += source[: int(m[1])].count("\n")
    return line, message[: m.start()]


def _placement(entry: Entry) -> tuple[str | None, str]:
    raw = entry.raw
    if "before" in raw and "after" in raw:
        return raw["before"], "before and after"
    if "before" in raw:
        return raw["before"], "before"
    if "after" in raw:
        return raw["after"], "after"
    return None, ""


def _check_target(rid: str, target: Any, key: str, locked_ids: set[str],
                  known: set[str]) -> None:
    if not isinstance(target, str):
        raise _Bad(f"rule {rid}: {key} names a rule id", key)
    if target == rid:
        raise _Bad(f"rule {rid}: {key} names the rule itself", key)
    if target in locked_ids:
        raise _Bad(f"rule {rid}: {key} = {target!r}: a locked rule cannot be preceded "
                   "or followed by a user rule", key)
    if target not in known:
        raise _Bad(f"rule {rid}: {key} = {target!r}: no such rule"
                   + _close(target, known), key)


def _cycle(rid: str, targets: Mapping[str, str | None]) -> bool:
    seen = {rid}
    nxt = targets.get(rid)
    while nxt is not None and nxt in targets:
        if nxt in seen:
            return True
        seen.add(nxt)
        nxt = targets[nxt]
    return False


__all__ = ["Composed", "Entry", "FileRules", "compile_rule", "compose", "parse_file"]
