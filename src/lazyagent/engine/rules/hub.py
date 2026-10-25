"""The engine's rule files (proposal 0014, "Files, precedence, validation
and errors"): which files there are, which version of each is applied, and
the effective catalog they compose to.

- **Global** ``$XDG_CONFIG_HOME/lazyagent/rules.toml``: the user's own, always
  trusted.
- **Repo** ``<repo>/.lazyagent/rules.toml``: anyone who commits to the repo
  writes it, so it applies only once the user has **allowed** its content
  hash (``rules_allow``, ``lazyagent rules allow``); a changed file stays on
  its last allowed version until it is allowed again (Q-R5).

A file that does not parse at all keeps its **last good** version, in memory
and as a copy under ``state_dir()/rules/`` (so it survives a restart); a bad
rule is skipped alone (:mod:`.user`). The hub re-reads the files when they
change (:meth:`RulesHub.check`, on the engine's tick) and emits
``rules.changed`` and ``rules.errors``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lazyagent.engine.rules.catalog import Catalog
from lazyagent.engine.rules.user import FileRules, compose, parse_file, problem

log = logging.getLogger(__name__)

RULES_CHANGED = "rules.changed"
RULES_ERRORS = "rules.errors"
REPO_FILE = os.path.join(".lazyagent", "rules.toml")
# The shortest hash prefix rules_allow takes.
MIN_HASH = 12


def global_path() -> Path:
    """``$XDG_CONFIG_HOME/lazyagent/rules.toml``."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lazyagent" / "rules.toml"


def repo_path(repo_root: str) -> Path:
    return Path(repo_root) / REPO_FILE


def store_dir() -> Path:
    from lazyagent.engine.runtime import state_dir

    return state_dir() / "rules"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _write(path: Path, text: str) -> None:
    from lazyagent.engine.notes import _atomic_write

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _atomic_write(path, text.encode())
    except OSError as e:
        log.warning("could not write %s: %s", path, e)


class RulesHub:
    """One engine's rule files and effective catalog."""

    def __init__(self, repo_root: Callable[[], str] | str = "",
                 emit: Callable[[str, Any], Any] | None = None) -> None:
        self._root = repo_root if callable(repo_root) else (lambda: repo_root)
        self._emit = emit
        self._catalog: Catalog | None = None
        self._signature: Any = None
        self.errors: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        # scope → the last text that parsed (and, for the repo, was allowed).
        self._good: dict[str, str] = {}
        self._shape: Any = None

    # --- paths ---

    @property
    def repo_root(self) -> str:
        return self._root() or ""

    def _repo_store(self) -> Path:
        from lazyagent.engine.runtime import repo_digest

        return store_dir() / f"repo-{repo_digest(self.repo_root)}.toml"

    def _allowed_path(self) -> Path:
        return store_dir() / "allowed.json"

    def _allowed(self) -> dict[str, str]:
        text = _read(self._allowed_path())
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data, dict) else {}

    # --- reading ---

    @property
    def catalog(self) -> Catalog:
        """The effective catalog (loaded on first use)."""
        if self._catalog is None:
            self.reload()
        assert self._catalog is not None
        return self._catalog

    def _sign(self) -> Any:
        root = self.repo_root
        return (
            root,
            _stat(global_path()),
            _stat(repo_path(root)) if root else None,
            _stat(self._allowed_path()),
        )

    def check(self) -> bool:
        """Re-read the files if they changed since the last load; True when
        the effective catalog or the errors changed."""
        if self._catalog is not None and self._sign() == self._signature:
            return False
        return self.reload()

    def reload(self) -> bool:
        from lazyagent.engine.rules import builtin

        self._signature = self._sign()
        files: list[FileRules] = []
        infos: list[dict[str, Any]] = []
        extra: list[dict[str, Any]] = []
        g = self._load_global(infos, extra)
        if g is not None:
            files.append(g)
        root = self.repo_root
        if root:
            r = self._load_repo(root, infos, extra)
            if r is not None:
                files.append(r)
        composed = compose(builtin(), files)
        self._catalog = composed.catalog
        before = (self.errors, self._shape)
        self.errors = extra + composed.errors
        self.warnings = composed.warnings
        self.files = infos
        self._shape = [
            (r.id, r.origin, r.file, r.line, r.when.source,
             r.and_when.source if r.and_when else None) for r in self._catalog.rules
        ]
        changed = before != (self.errors, self._shape)
        if self._emit is not None:
            if self._shape != before[1]:
                self._emit(RULES_CHANGED, {"files": self.files,
                                           "rules": len(self._catalog.rules)})
            if self.errors != before[0]:
                self._emit(RULES_ERRORS, {"errors": self.errors})
        return changed

    def _parsed(self, scope: str, path: Path, text: str, store: Path,
                extra: list[dict[str, Any]]) -> tuple[FileRules | None, str]:
        """``text`` parsed, or the last good version when it does not
        parse: ``(rules, "current" | "last_good" | "none")``."""
        f = parse_file(text, scope, str(path))
        if f.ok:
            if self._good.get(scope) != text:
                self._good[scope] = text
                _write(store, text)
            return f, "current"
        good = self._good.get(scope) or _read(store)
        for e in f.errors:
            e["fallback"] = "the last good version is in force" if good else "file ignored"
        extra.extend(f.errors)
        if not good:
            return None, "none"
        self._good[scope] = good
        g = parse_file(good, scope, str(path))
        return (g, "last_good") if g.ok else (None, "none")

    def _load_global(self, infos: list[dict[str, Any]],
                     extra: list[dict[str, Any]]) -> FileRules | None:
        path = global_path()
        text = _read(path)
        info: dict[str, Any] = {"scope": "global", "path": str(path), "exists": text is not None,
                                "hash": None, "applied": "none", "rules": 0}
        infos.append(info)
        if text is None:
            return None
        info["hash"] = digest(text)
        f, info["applied"] = self._parsed("global", path, text, store_dir() / "global.toml",
                                          extra)
        info["rules"] = len(f.entries) if f is not None else 0
        return f

    def _load_repo(self, root: str, infos: list[dict[str, Any]],
                   extra: list[dict[str, Any]]) -> FileRules | None:
        path = repo_path(root)
        text = _read(path)
        info: dict[str, Any] = {"scope": "repo", "path": str(path), "exists": text is not None,
                                "hash": None, "allowed": False, "applied": "none", "rules": 0}
        infos.append(info)
        if text is None:
            return None
        h = info["hash"] = digest(text)
        allowed = self._allowed().get(root)
        info["allowed"] = allowed == h
        store = self._repo_store()
        if allowed == h:
            f, info["applied"] = self._parsed("repo", path, text, store, extra)
        else:
            good = self._good.get("repo") or _read(store) if allowed else None
            f = parse_file(good, "repo", str(path)) if good else None
            if f is not None and not f.ok:
                f = None
            info["applied"] = "last_allowed" if f is not None else "none"
            what = "changed since you allowed it" if allowed else "not allowed yet"
            fallback = ("the last allowed version is in force" if f is not None
                        else "not applied")
            dummy = FileRules("repo", str(path))
            extra.append(problem(
                dummy, None, None,
                f"{what}: check it, then lazyagent rules allow {h[:MIN_HASH]}", fallback,
            ) | {"pending": h})
        info["rules"] = len(f.entries) if f is not None else 0
        return f

    # --- the user's allow ---

    def allow(self, hash_: str) -> dict[str, Any]:
        """Allow the repo file whose content hash starts with ``hash_``."""
        root = self.repo_root
        if not root:
            raise ValueError("The engine has no repo")
        if not isinstance(hash_, str) or len(hash_) < MIN_HASH:
            raise ValueError(f"hash must be the file's hash (at least {MIN_HASH} characters)")
        text = _read(repo_path(root))
        if text is None:
            raise ValueError(f"No {REPO_FILE} in {root}")
        h = digest(text)
        if not h.startswith(hash_.lower()):
            raise ValueError(
                f"{REPO_FILE} is at {h[:MIN_HASH]}, not {hash_[:MIN_HASH]}: it changed; "
                "check it again (lazyagent rules check)"
            )
        allowed = self._allowed()
        allowed[root] = h
        _write(self._allowed_path(), json.dumps(dict(sorted(allowed.items())), indent=1))
        self.reload()
        return {"repo": root, "file": str(repo_path(root)), "hash": h, "allowed": True}

    def check_result(self) -> dict[str, Any]:
        """``rules_check``: the errors, warnings and files."""
        self.catalog  # noqa: B018 - loads on first use
        return {"errors": self.errors, "warnings": self.warnings, "files": self.files}
