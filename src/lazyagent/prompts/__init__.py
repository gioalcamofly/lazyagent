"""Install lazyagent's usage guidance into the user's own agent prompts.

Three kinds of file, per target CLI (Claude Code, Codex, Gemini CLI):

- **skill**: ``<dir>/skills/lazyagent/SKILL.md``, rendered from
  ``templates/skill.md``, so it matches the installed lazyagent. The whole
  file is lazyagent's; a stamp line after its front matter
  (``<!-- lazyagent:generated v0.8.0 h=... -->``) records the version and a
  hash of the rest, so a hand edit is told apart from an older version.
- **pointer**: a short block at the end of the global instructions file
  (``~/.claude/CLAUDE.md``, ``~/.codex/AGENTS.md``, ``~/.gemini/GEMINI.md``;
  with scope ``repo``, the repo's own ``CLAUDE.md`` / ``AGENTS.md`` /
  ``GEMINI.md``) that names the skill instead of copying it.
- **skill_line**: a one-line block in a skill of the user's choosing (an
  orchestrator or planner skill) telling it to follow the lazyagent skill.

Blocks sit between ``<!-- lazyagent:begin v<version> h=<hash> -->`` and
``<!-- lazyagent:end -->``: an update replaces only the block, and the hash
(of the block's body) tells a hand-edited block from an outdated one.

Nothing is written without a :class:`Change` the caller showed first:
:func:`plan` gives each file's diff and the sha256 of the file it was
computed from (``base_sha``) and of the result (``new_sha``); :func:`apply`
re-plans that one file and refuses when either differs (the file changed
since the preview, or lazyagent did). Every write saves a backup under
``$XDG_STATE_HOME/lazyagent/prompts/backups`` first and is atomic. A file
that resolves outside its target dir (a symlink) is written only with
``follow_symlinks``; replacing or removing a hand-edited block or skill only
with ``force``.

The CLI (:mod:`lazyagent.prompts.cli`) calls these directly; the desktop
goes through the engine methods ``prompts_status`` / ``prompts_plan`` /
``prompts_apply``, which call the same functions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from lazyagent import __version__

SCOPES = ("global", "repo")
ACTIONS = ("install", "uninstall")
KINDS = ("skill", "pointer", "skill_line")
SKILL_NAME = "lazyagent"
ABSENT = "absent"

# File states (status): absent, installed, outdated, modified.
INSTALLED = "installed"
OUTDATED = "outdated"
MODIFIED = "modified"
_RANK = {INSTALLED: 0, ABSENT: 1, OUTDATED: 2, MODIFIED: 3}

_BEGIN_RE = re.compile(r"^<!-- lazyagent:begin v(\S+?)(?: h=([0-9a-f]+))? -->$")
_END = "<!-- lazyagent:end -->"
_STAMP_RE = re.compile(r"^<!-- lazyagent:generated v(\S+?) h=([0-9a-f]+) -->$")
_SKILL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HASH_LEN = 12
NOT_OURS = "not written by lazyagent"


@dataclass(frozen=True)
class Spec:
    name: str
    title: str
    dir_env: str | None  # the env var that moves its config dir
    dir_name: str  # under HOME, and in a repo (skills)
    instructions: str
    binary: str


TARGETS: dict[str, Spec] = {
    "claude": Spec("claude", "Claude Code", "CLAUDE_CONFIG_DIR", ".claude", "CLAUDE.md", "claude"),
    "codex": Spec("codex", "Codex", "CODEX_HOME", ".codex", "AGENTS.md", "codex"),
    "gemini": Spec("gemini", "Gemini CLI", None, ".gemini", "GEMINI.md", "gemini"),
}


class ChangedError(ValueError):
    """The file (or lazyagent) changed between the preview and the approval."""


@dataclass(frozen=True)
class Env:
    """Where the prompts live: HOME, the environment (config dir overrides,
    ``XDG_STATE_HOME``), the repo (scope ``repo``) and the version to render."""

    home: Path
    environ: Mapping[str, str] = field(default_factory=dict)
    repo: Path | None = None
    version: str = __version__
    which: Callable[[str], str | None] = shutil.which

    @classmethod
    def process(cls, repo: str | Path | None = None) -> Env:
        return cls(Path.home(), dict(os.environ), Path(repo) if repo else None)

    def config_dir(self, spec: Spec) -> Path:
        moved = self.environ.get(spec.dir_env) if spec.dir_env else None
        return Path(moved).expanduser() if moved else self.home / spec.dir_name

    def root(self, spec: Spec, scope: str) -> Path:
        """The target dir: writes resolving outside it are flagged."""
        if scope == "repo":
            if self.repo is None:
                raise ValueError("scope repo needs a repo")
            return self.repo
        return self.config_dir(spec)

    def skills_dir(self, spec: Spec, scope: str) -> Path:
        base = self.root(spec, scope) / spec.dir_name if scope == "repo" else self.config_dir(spec)
        return base / "skills"

    def instructions_file(self, spec: Spec, scope: str) -> Path:
        return self.root(spec, scope) / spec.instructions

    def backups_dir(self) -> Path:
        base = self.environ.get("XDG_STATE_HOME") or str(self.home / ".local" / "state")
        return Path(base).expanduser() / "lazyagent" / "prompts" / "backups"

    def display(self, path: Path, scope: str) -> str:
        if scope == "repo" and self.repo is not None and _within(path, self.repo):
            return str(path.relative_to(self.repo))
        if _within(path, self.home):
            return "~/" + str(path.relative_to(self.home))
        return str(path)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _check_scope(scope: str) -> None:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}, not {scope!r}")


def _sha(text: str | None) -> str:
    return ABSENT if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LEN]


# ----------------------------------------------------------------------
# Targets
# ----------------------------------------------------------------------


def target_info(env: Env, name: str) -> dict[str, Any]:
    """Whether the CLI is there: its config dir exists (it ran here), and
    whether its binary is on PATH."""
    spec = _spec(name)
    directory = env.config_dir(spec)
    binary = env.which(spec.binary)
    configured = directory.is_dir()
    if configured:
        reason = f"{env.display(directory, 'global')} exists"
    else:
        reason = f"no {env.display(directory, 'global')}"
    reason += f"; `{spec.binary}` {'on PATH' if binary else 'not on PATH'}"
    return {"name": name, "title": spec.title, "dir": str(directory), "detected": configured,
            "binary": binary, "reason": reason}


def _spec(name: str) -> Spec:
    try:
        return TARGETS[name]
    except KeyError:
        raise ValueError(f"unknown target {name!r} (claude, codex, gemini)") from None


def resolve_targets(env: Env, targets: Iterable[str] | str | None) -> list[str]:
    """``None`` / ``"detected"``: the targets whose config dir exists;
    ``"all"``: all three; else the names given (``"a,b"`` or a list)."""
    if targets is None or targets == "detected":
        return [n for n in TARGETS if target_info(env, n)["detected"]]
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    names = list(targets)
    if "all" in names:
        return list(TARGETS)
    for n in names:
        _spec(n)
    return [n for n in TARGETS if n in names]


def available_skills(env: Env, target: str, scope: str = "global") -> list[str]:
    """The user's skills in the target's skills dir (each a dir with a
    SKILL.md), lazyagent's own left out."""
    directory = env.skills_dir(_spec(target), scope)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    return [e.name for e in entries
            if e.name != SKILL_NAME and _SKILL_RE.match(e.name) and (e / "SKILL.md").is_file()]


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------


def _template(name: str) -> str:
    return resources.files("lazyagent.prompts").joinpath("templates", name).read_text(encoding="utf-8")


def render_skill(version: str) -> str:
    """The skill file: front matter, the stamp, the body."""
    template = _template("skill.md")
    bare = template.replace("{{stamp}}\n", "")
    return template.replace("{{stamp}}", f"<!-- lazyagent:generated v{version} h={_h(bare)} -->")


def _skill_ref(env: Env, spec: Spec, scope: str) -> str:
    return env.display(env.skills_dir(spec, scope) / SKILL_NAME / "SKILL.md", scope)


def render_block(kind: str, version: str, skill_ref: str) -> str:
    """A pointer or skill_line block, markers included, ending in a newline."""
    body = _template("pointer.md" if kind == "pointer" else "skill_line.md")
    body = body.replace("{{skill_ref}}", skill_ref).rstrip("\n")
    return f"<!-- lazyagent:begin v{version} h={_h(body)} -->\n{body}\n{_END}\n"


@dataclass(frozen=True)
class _Block:
    start: int  # line index of the begin marker
    stop: int  # line index of the end marker
    version: str
    hash: str | None
    body: str


def find_block(text: str) -> _Block | None:
    """The one lazyagent block in ``text``, or None. Raises ValueError for
    markers that do not pair up (a begin with no end, two blocks)."""
    lines = text.splitlines()
    begins = [i for i, line in enumerate(lines) if line.startswith("<!-- lazyagent:begin")]
    ends = [i for i, line in enumerate(lines) if line.strip() == _END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise ValueError("its lazyagent markers do not pair up (fix them by hand)")
    m = _BEGIN_RE.match(lines[begins[0]].strip())
    if m is None:
        raise ValueError("its lazyagent begin marker is malformed (fix it by hand)")
    body = "\n".join(lines[begins[0] + 1:ends[0]])
    return _Block(begins[0], ends[0], m.group(1), m.group(2), body)


# A file whose text is what this version renders is installed even when an
# older version stamped it: an upgrade that changes nothing in it is no news
# (and no write).


def _block_state(block: _Block, rendered: str) -> str:
    if block.hash != _h(block.body):
        return MODIFIED
    want = find_block(rendered)
    assert want is not None
    return INSTALLED if block.body == want.body else OUTDATED


def _unstamped(text: str) -> tuple[str, re.Match[str] | None]:
    """The skill file without its stamp line, and the stamp."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = _STAMP_RE.match(line.strip())
        if m:
            return "".join(lines[:i] + lines[i + 1:]), m
    return text, None


def _skill_state(text: str, rendered: str) -> tuple[str, str | None, str]:
    """(state, stamped version, note) of an existing skill file."""
    rest, m = _unstamped(text)
    if m is None:
        return MODIFIED, None, NOT_OURS
    if _h(rest) != m.group(2):
        return MODIFIED, m.group(1), "edited by hand"
    return (INSTALLED if rest == _unstamped(rendered)[0] else OUTDATED), m.group(1), ""


def _insert(text: str | None, block: str) -> str:
    if text is None or not text.strip():
        return block
    if not text.endswith("\n"):
        text += "\n"
    return text + "\n" + block


def _replace(text: str, found: _Block, block: str) -> str:
    lines = text.splitlines(keepends=True)
    return "".join(lines[:found.start]) + block + "".join(lines[found.stop + 1:])


def _remove(text: str, found: _Block) -> str | None:
    """The text without the block (and the blank line before it that
    install added); None when nothing else is left."""
    lines = text.splitlines(keepends=True)
    before, after = lines[:found.start], lines[found.stop + 1:]
    if before and not before[-1].strip() and (not after or not after[0].strip()):
        before = before[:-1]
    out = "".join(before + after)
    return out if out.strip() else None


# ----------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------


@dataclass
class Change:
    """One file: what it holds now, what it would hold, and why."""

    id: str
    target: str
    scope: str
    kind: str
    skill: str | None
    path: Path
    real: Path
    display: str
    state: str  # absent | installed | outdated | modified
    action: str  # create | update | remove | none | blocked
    old: str | None
    new: str | None
    outside: str | None = None  # the real path, when it resolves outside the target dir
    version: str | None = None  # the version the file is stamped with
    note: str = ""

    @property
    def base_sha(self) -> str:
        return _sha(self.old)

    @property
    def new_sha(self) -> str:
        return _sha(self.new)

    @property
    def needs_force(self) -> bool:
        return self.state == MODIFIED and self.action in ("update", "remove")

    def diff(self) -> str:
        if self.action in ("none", "blocked"):
            return ""
        old = (self.old or "").splitlines(keepends=True)
        new = (self.new or "").splitlines(keepends=True)
        a = self.display if self.old is not None else "/dev/null"
        b = self.display if self.new is not None else "/dev/null"
        out = "".join(difflib.unified_diff(old, new, a, b))
        return out if out.endswith("\n") or not out else out + "\n"

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "target": self.target, "scope": self.scope, "kind": self.kind,
                "skill": self.skill, "path": str(self.path), "real_path": str(self.real),
                "display": self.display, "state": self.state, "action": self.action,
                "outside": self.outside, "version": self.version, "note": self.note,
                "needs_force": self.needs_force, "base_sha": self.base_sha,
                "new_sha": self.new_sha, "diff": self.diff()}


def _read(path: Path, real: Path) -> tuple[str | None, str]:
    """(text, problem): the file's text (None: absent), or why it cannot
    be used."""
    if not os.path.lexists(path):
        return None, ""
    if not real.exists():
        return None, f"a dangling symlink to {real}"
    if not real.is_file():
        return None, "not a regular file"
    try:
        return real.read_bytes().decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    except OSError as e:
        return None, f"cannot read it: {e.strerror or e}"


def _locate(env: Env, spec: Spec, scope: str, path: Path) -> tuple[Path, str | None]:
    real = Path(os.path.realpath(path))
    root = Path(os.path.realpath(env.root(spec, scope)))
    return real, (None if _within(real, root) else str(real))


def change_for(env: Env, change_id: str, action: str) -> Change:
    """The change ``action`` makes to the one file ``change_id`` names
    (``<target>:<scope>:skill``, ``…:pointer``, ``…:skill_line:<name>``)."""
    if action not in ACTIONS:
        raise ValueError(f"action must be install or uninstall, not {action!r}")
    parts = change_id.split(":")
    if len(parts) < 3 or parts[2] not in KINDS or (parts[2] == "skill_line") != (len(parts) == 4):
        raise ValueError(f"bad change id {change_id!r}")
    target, scope, kind = parts[:3]
    skill = parts[3] if len(parts) == 4 else None
    spec = _spec(target)
    _check_scope(scope)
    if skill is not None and (not _SKILL_RE.match(skill) or skill == SKILL_NAME):
        raise ValueError(f"bad skill name {skill!r}")
    if kind == "skill":
        path = env.skills_dir(spec, scope) / SKILL_NAME / "SKILL.md"
    elif kind == "pointer":
        path = env.instructions_file(spec, scope)
    else:
        path = env.skills_dir(spec, scope) / skill / "SKILL.md"
    real, outside = _locate(env, spec, scope, path)
    old, problem = _read(path, real)
    c = Change(change_id, target, scope, kind, skill, path, real, env.display(path, scope),
               ABSENT, "none", old, old, outside)
    if problem:
        c.action, c.note = "blocked", problem
        return c
    if kind == "skill":
        rendered = render_skill(env.version)
        if old is not None:
            c.state, c.version, c.note = _skill_state(old, rendered)
        if action == "install":
            c.new = old if c.state == INSTALLED else rendered
        elif c.note == NOT_OURS:
            c.action = "blocked"
            return c
        elif old is not None:
            c.new = None
    else:
        if kind == "skill_line" and old is None:
            c.action, c.note = "blocked", "no such skill"
            return c
        rendered = render_block(kind, env.version, _skill_ref(env, spec, scope))
        try:
            found = find_block(old or "")
        except ValueError as e:
            c.state, c.action, c.note = MODIFIED, "blocked", str(e)
            return c
        if found is not None:
            c.state, c.version = _block_state(found, rendered), found.version
            if c.state == MODIFIED:
                c.note = "the block was edited by hand"
        if action == "install":
            if c.state != INSTALLED:
                c.new = _insert(old, rendered) if found is None else _replace(old or "", found, rendered)
        elif found is not None:
            c.new = _remove(old or "", found)
    if c.new == c.old:
        c.action = "none"
    elif c.new is None:
        c.action = "remove"
    else:
        c.action = "create" if c.old is None else "update"
    return c


def plan(
    env: Env,
    action: str = "install",
    targets: Iterable[str] | str | None = None,
    scope: str = "global",
    skills: Iterable[str] = (),
) -> list[Change]:
    """Every file ``action`` touches, in order: per target the skill, the
    pointer, then skill lines (install: the ``skills`` named, plus those
    already carrying a block, to update them; uninstall: every skill with a
    block). A named skill none of the targets has is an error."""
    _check_scope(scope)
    wanted = [s.strip() for s in skills if s.strip()]
    for s in wanted:
        if not _SKILL_RE.match(s) or s == SKILL_NAME:
            raise ValueError(f"bad skill name {s!r}")
    found: set[str] = set()
    out: list[Change] = []
    for name in resolve_targets(env, targets):
        base = f"{name}:{scope}"
        out.append(change_for(env, f"{base}:skill", action))
        out.append(change_for(env, f"{base}:pointer", action))
        have = available_skills(env, name, scope)
        found.update(s for s in wanted if s in have)
        for skill in have:
            c = change_for(env, f"{base}:skill_line:{skill}", action)
            if (action == "install" and skill in wanted) or c.state != ABSENT:
                out.append(c)
    missing = [s for s in wanted if s not in found]
    if missing:
        raise ValueError(f"no skill named {', '.join(missing)} in the targets' skills dirs")
    return out


# ----------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------


def apply(
    env: Env,
    change_id: str,
    action: str,
    base_sha: str,
    new_sha: str,
    *,
    follow_symlinks: bool = False,
    force: bool = False,
    stamp: str | None = None,
) -> dict[str, Any]:
    """Write one previewed change. Refuses (ValueError) when the file or
    the rendering changed since the preview (:class:`ChangedError`), when it
    resolves outside its target dir without ``follow_symlinks``, or when it
    would replace or remove a hand-edited block without ``force``. Saves a
    backup first (``stamp``: the backups' subdir, one per run)."""
    c = change_for(env, change_id, action)
    if c.action == "blocked":
        raise ValueError(f"{c.display}: {c.note}")
    if base_sha != c.base_sha:
        raise ChangedError(f"{c.display} changed since the preview: preview it again")
    if new_sha != c.new_sha:
        raise ChangedError(f"{c.display}: what lazyagent would write changed since the preview: preview it again")
    if c.action == "none":
        return c.to_json() | {"written": False, "backup": None}
    if c.outside and not follow_symlinks:
        raise ValueError(f"{c.display} resolves outside its dir, to {c.outside}: approve following the symlink")
    if c.needs_force and not force:
        raise ValueError(f"{c.display}: {c.note or 'edited by hand'}; approve replacing it to go on")
    backup = _backup(env, c, stamp or new_stamp())
    if c.new is None:
        os.unlink(c.real)
        if c.kind == "skill":
            # Its dir, then the skills dir, when nothing else is in them.
            for directory in (c.real.parent, c.real.parent.parent):
                try:
                    directory.rmdir()
                except OSError:
                    break
    else:
        _write_atomic(c.real, c.new)
    return c.to_json() | {"written": True, "backup": str(backup) if backup else None}


def new_stamp() -> str:
    now = time.time()
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1e6) % 1_000_000:06d}"


def _backup(env: Env, c: Change, stamp: str) -> Path | None:
    """Copy the file as it is (if it exists) under the backups dir, and log
    the write in ``log.jsonl`` there."""
    root = env.backups_dir()
    copy = None
    if c.old is not None:
        copy = root / stamp / str(c.real).lstrip("/")
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c.real, copy)
    root.mkdir(parents=True, exist_ok=True)
    entry = {"at": time.time(), "id": c.id, "path": str(c.path), "real_path": str(c.real),
             "action": c.action, "existed": c.old is not None, "backup": str(copy) if copy else None,
             "version": env.version}
    with open(root / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return copy


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    tmp = path.with_name(f".{path.name}.lazyagent-{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644 if mode is None else 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------


def status(env: Env, targets: Iterable[str] | str | None = "all", scope: str = "global") -> dict[str, Any]:
    """Per target: detected or not, each file's state, and the target's
    (the worst of its files: modified, outdated, then absent; a target with
    some files installed and some absent is outdated: install completes it)."""
    _check_scope(scope)
    out = []
    for name in resolve_targets(env, targets):
        info = target_info(env, name)
        files = [c for c in plan(env, "install", [name], scope) if c.kind != "skill_line" or c.state != ABSENT]
        states = {c.state for c in files}
        if states <= {ABSENT}:
            state, note = ABSENT, ""
        else:
            state = max(states, key=lambda s: _RANK[s])
            note = ""
            if state == ABSENT:
                state = OUTDATED
            if ABSENT in states:
                note = "incomplete: " + ", ".join(c.display for c in files if c.state == ABSENT)
        out.append(info | {"state": state, "note": note, "skills": available_skills(env, name, scope),
                           "files": [_file_json(c) for c in files]})
    return {"version": env.version, "scope": scope, "targets": out}


def _file_json(c: Change) -> dict[str, Any]:
    return {"id": c.id, "kind": c.kind, "skill": c.skill, "path": str(c.path), "display": c.display,
            "state": c.state, "version": c.version, "outside": c.outside, "note": c.note}


def notice(env: Env) -> dict[str, Any] | None:
    """"Your prompts are outdated" after an upgrade: the installed files
    (global scope, and the repo's when ``env.repo`` is set) stamped with
    another version or rendered differently. None when nothing is
    outdated, or nothing is installed."""
    ids: list[str] = []
    versions: set[str] = set()
    for scope in ("global", "repo") if env.repo is not None else ("global",):
        for t in status(env, "all", scope)["targets"]:
            for f in t["files"]:
                stale = f["state"] == OUTDATED or (
                    f["state"] == MODIFIED and f["version"] not in (None, env.version))
                if stale:
                    ids.append(f["id"])
                    if f["version"]:
                        versions.add(f["version"])
    if not ids:
        return None
    was = ", ".join(sorted(versions))
    since = f" (v{was} → v{env.version})" if was else ""
    return {"ids": ids, "version": env.version,
            "text": f"Your lazyagent prompts are outdated{since}: lazyagent prompts install"}
