"""Clean merges of the base branch, which ``pr.commits_after_ready`` does not
count (docs/design/rules-plan.md R3c).

A commit after the PR's ready point is a **clean base merge** when it is a
merge commit with two parents, one of them on the base branch (an ancestor
of ``origin/<base>`` or ``<base>``), and its tree is exactly the automatic
merge of its parents: what "Update branch" or a ``git merge origin/main``
with no conflicts makes. A conflict resolved by hand or an extra edit
changes the tree, so it counts; so do a merge of any other branch, an
octopus merge and every normal commit.

**The automatic merge**, local git only, never a working tree:

- git ≥ 2.38: ``git merge-tree --write-tree <p1> <p2>`` (merge-ort, renames
  and several merge bases included).
- older git: a temporary index (``GIT_INDEX_FILE``) and ``git read-tree -i
  -m --aggressive <base> <p1> <p2>``; each path still unmerged is merged
  with ``git merge-file -p`` on its three blobs (a mode that changed on one
  side takes that side's), then ``git write-tree``. A content conflict, a
  path deleted on one side and changed on the other, or one added on both
  sides is not clean. Undecidable, so counted: several merge bases (merge-ort
  would build a virtual one), a binary file, a symlink or submodule changed
  on both sides, a mode changed on both sides. read-tree sees no renames: a
  rename on one side with an edit on the other reads as a conflict, so it is
  counted (never the other way round, since the result must equal the
  commit's tree).

**Generated files** (R4c): with ``[review] generated_paths`` (globs, ``**``
across directories, matched against the whole path), a merge of the base
whose tree differs from the automatic merge only in paths that match counts
as clean too (``base merge, only generated files differ``): the conflicts
were confined to generated files, resolved by merging them or regenerating,
with no other edit. Both ways find the paths: merge-tree lists the conflicted
ones (``--name-only``; its tree holds them with markers); the old way merges
every path it can, drops the ones it cannot from the index and counts them
as differing. The commit's tree is compared with that tree path by path
(``git diff-tree -r --no-renames``). Without the key a conflict still ends
the check early, as before.

Both write their objects into a temporary object directory (the repo's own
as an alternate), so the repository is never written to.

**Missing commits.** A merge GitHub made ("Update branch") is not local
until fetched: one ``git fetch --no-tags --no-write-fetch-head origin <sha>
<base>`` (which also moves ``origin/<base>``, as any fetch of it does), at
most once per commit every :data:`FETCH_EVERY` seconds, and never on git
older than 2.29 (no ``--no-write-fetch-head``). Still missing: counted.

**Force pushes and a moved head.** A head that moved from ``before`` to
``after`` (a force push, or the head check of ``WorktreeService``) moved
only by clean base merges when every commit in ``after`` and not in
``before`` or the base, and every commit dropped the other way, is one.

Results are cached per commit (and base); nothing here calls GitHub's API.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from lazyagent import diagnostics as diag
from lazyagent.models import PrInfo

log = logging.getLogger(__name__)

__all__ = ["BaseMerges", "git_version"]

FETCH_EVERY = 600.0          # seconds between fetch attempts for one commit
MAX_RANGE = 200              # commits between two heads worth classifying
_GIT_TIMEOUT = 20
_FETCH_TIMEOUT = 30
_CACHE_MAX = 4096
_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_VERSION_RE = re.compile(r"git version (\d+)\.(\d+)")
CLEAN = "clean base merge"
GENERATED = "base merge, only generated files differ"

# A verdict: True, a clean base merge (not counted); False, it counts.
# ``final`` verdicts are kept for good; the others (a commit or the base not
# here yet) are tried again once a fetch may have brought them.
_Verdict = tuple[bool, str, bool]


def git_version(run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                ) -> tuple[int, int] | None:
    """``(major, minor)`` of the git on PATH; None when it cannot tell."""
    try:
        out = run(["git", "--version"], capture_output=True, text=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    found = _VERSION_RE.search(out.stdout or "")
    return (int(found.group(1)), int(found.group(2))) if found else None


def _is_sha(value: str) -> bool:
    return bool(_SHA_RE.match(value or ""))


def _glob(pattern: str) -> re.Pattern[str]:
    """A path glob: ``**`` any directories, ``*`` and ``?`` within one."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i + 1)
            if j < 0:
                out.append(re.escape("["))
                i += 1
            else:
                out.append(fnmatch.translate(pattern[i:j + 1])[4:-3])
                i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def generated(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_glob(p).match(path) for p in patterns)


class BaseMerges:
    """Classifies merge commits for ``pr.commits_after_ready``; thread-safe.

    ``version`` forces the git version (tests); ``fetch=False`` never
    fetches. ``clock`` drives the fetch back-off.
    """

    def __init__(self, *, version: tuple[int, int] | None = None, fetch: bool = True,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._version = version
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._verdicts: dict[tuple[str, str, str, tuple[str, ...]], _Verdict] = {}
        # [review] generated_paths: the rules files' (the engine sets
        # paths_of to read them), else what was set.
        self.paths_of: Callable[[], tuple[str, ...]] | None = None
        self._paths: tuple[str, ...] = ()
        self._moves: dict[tuple[str, str, str, str, tuple[str, ...]], int] = {}
        self._fetched: dict[tuple[str, str], float] = {}
        self._objects: dict[str, str] = {}
        self.git_calls = 0        # for the cost measurement and the tests

    # --- the public part ------------------------------------------------

    @property
    def generated_paths(self) -> tuple[str, ...]:
        return tuple(self.paths_of()) if self.paths_of is not None else self._paths

    @generated_paths.setter
    def generated_paths(self, paths: tuple[str, ...]) -> None:
        self._paths = tuple(paths)

    @property
    def version(self) -> tuple[int, int] | None:
        if self._version is None:
            self._version = git_version() or (0, 0)
        return self._version

    def is_clean_base_merge(self, root: str, sha: str, base: str) -> bool:
        """``sha`` is a merge of ``base`` whose tree is its automatic merge."""
        return self.verdict(root, sha, base)[0]

    def verdict(self, root: str, sha: str, base: str) -> tuple[bool, str]:
        """``(clean, why)``: why it counts, or :data:`CLEAN` /
        :data:`GENERATED`."""
        if not root or not _is_sha(sha) or not base or base.startswith("-"):
            return False, "not a commit or no base"
        key = (root, sha, base, self.generated_paths)
        with self._lock:
            got = self._verdicts.get(key)
        if got is None or not got[2]:
            got = self._classify(root, sha, base)
            with self._lock:
                if len(self._verdicts) >= _CACHE_MAX:
                    self._verdicts.clear()
                self._verdicts[key] = got
        return got[0], got[1]

    def moved_only_by_base_merges(self, root: str, before: str, after: str,
                                  base: str) -> int | None:
        """How many clean base merges the head gained from ``before`` to
        ``after`` when every commit it gained or lost (leaving the base's
        own aside) is one; None when any is not, or it cannot tell."""
        if not root or not _is_sha(before) or not _is_sha(after) or not base \
                or base.startswith("-"):
            return None
        if before == after:
            return 0
        key = (root, before, after, base, self.generated_paths)
        with self._lock:
            if key in self._moves:
                return self._moves[key]
        if not (self._have(root, before) and self._have(root, after)):
            self._fetch_once(root, (before, after), base)
        refs = self._base_refs(root, base)
        if not refs:
            return None
        gained = self._rev_list(root, after, before, *refs)
        lost = self._rev_list(root, before, after, *refs)
        if gained is None or lost is None or len(gained) + len(lost) > MAX_RANGE:
            return None
        if not all(self.is_clean_base_merge(root, c, base) for c in (*gained, *lost)):
            return None
        with self._lock:
            if len(self._moves) >= _CACHE_MAX:
                self._moves.clear()
            self._moves[key] = len(gained)
        return len(gained)

    def commits_since(self, root: str, since: str, head: str,
                      base: str) -> tuple[int, int, int] | None:
        """``(commits, clean base merges, those of them where only generated
        files differ)`` on ``head`` after ``since``, the clean merges not
        counted in the first (the base's own commits they bring are not
        counted either); None when ``since`` is not in ``head``'s history (a
        force push dropped it) or it cannot tell."""
        if not root or not _is_sha(since) or not _is_sha(head) or not base \
                or base.startswith("-"):
            return None
        if since == head:
            return 0, 0, 0
        if not (self._have(root, since) and self._have(root, head)):
            self._fetch_once(root, (since, head), base)
        if self._git(root, ["merge-base", "--is-ancestor", since, head])[0] != 0:
            return None
        commits = self._rev_list(root, head, since, *self._base_refs(root, base))
        if commits is None or len(commits) > MAX_RANGE:
            return None
        verdicts = [self.verdict(root, c, base) for c in commits]
        merges = sum(clean for clean, _ in verdicts)
        return len(commits) - merges, merges, sum(why == GENERATED for _, why in verdicts)

    def commit_list(self, root: str, since: str | None, head: str,
                    base: str) -> list[dict[str, str | None]] | None:
        """The commits on ``head`` after ``since`` (None: the PR's own, all
        that are not on the base), newest first, at most :data:`MAX_RANGE`:
        ``{sha, subject, author, date, base_merge}``, ``base_merge`` the
        verdict (:data:`CLEAN` or :data:`GENERATED`) of a merge of the base
        that is not counted, else None. The base's own commits a merge
        brings are left out. None when ``since`` is not in ``head``'s
        history (a force push dropped it) or it cannot tell."""
        if not root or not _is_sha(head) or not base or base.startswith("-") \
                or (since is not None and not _is_sha(since)):
            return None
        if since == head:
            return []
        wanted = (head,) if since is None else (since, head)
        if not all(self._have(root, s) for s in wanted):
            self._fetch_once(root, wanted, base)
        if since is not None and \
                self._git(root, ["merge-base", "--is-ancestor", since, head])[0] != 0:
            return None
        refs = self._base_refs(root, base)
        if since is None and not refs:
            return None
        code, out = self._git(root, ["log", "--no-show-signature", f"--max-count={MAX_RANGE}",
                                     "--format=%H%x1f%P%x1f%an%x1f%cI%x1f%s%x1e", head,
                                     "--not", *([since] if since else []), *refs])
        if code != 0:
            return None
        commits: list[dict[str, str | None]] = []
        for entry in filter(None, (e.strip("\n") for e in out.split("\x1e"))):
            sha, parents, author, date, subject = (entry.split("\x1f") + [""] * 5)[:5]
            clean, why = (self.verdict(root, sha, base) if len(parents.split()) == 2
                          else (False, ""))
            commits.append({"sha": sha, "subject": subject, "author": author, "date": date,
                            "base_merge": why if clean else None})
        return commits

    def adjust(self, root: str, info: PrInfo | None) -> PrInfo | None:
        """``info`` without the clean base merges (and the force pushes that
        moved the head only by them) in ``commits_after_ready``, and with
        ``base_merges_after_ready``. Unknown stays unknown."""
        if info is None or info.commits_after_ready is None:
            return info
        base = info.base_ref
        verdicts = [self.verdict(root, sha, base) for sha in info.after_ready_merges]
        merges = sum(clean for clean, _ in verdicts)
        gen = sum(why == GENERATED for _, why in verdicts)
        pushes = sum(self.moved_only_by_base_merges(root, b, a, base) is not None
                     for b, a in info.after_ready_pushes)
        after = max(0, info.commits_after_ready - merges - pushes)
        if after == info.commits_after_ready and info.base_merges_after_ready == merges \
                and info.generated_merges_after_ready == gen:
            return info
        return replace(info, commits_after_ready=after, base_merges_after_ready=merges,
                       generated_merges_after_ready=gen)

    # --- classification -------------------------------------------------

    def _classify(self, root: str, sha: str, base: str) -> _Verdict:
        start = time.perf_counter()
        got = self._classify_once(root, sha, base)
        if not got[2] and self._fetch_once(root, (sha,), base):
            got = self._classify_once(root, sha, base)
        diag.event(log, "base_merges.classified", sha=sha[:10], base=base, clean=got[0],
                   why=got[1], ms=round((time.perf_counter() - start) * 1000, 1))
        return got

    def _classify_once(self, root: str, sha: str, base: str) -> _Verdict:
        code, out = self._git(root, ["rev-list", "--parents", "-n", "1", sha])
        if code != 0 or not out.split():
            return False, "the commit is not here", False
        parents = out.split()[1:]
        if len(parents) != 2:
            return False, "not a merge" if len(parents) < 2 else "an octopus merge", True
        refs = self._base_refs(root, base)
        if not any(self._git(root, ["merge-base", "--is-ancestor", p, ref])[0] == 0
                   for p in parents for ref in refs):
            return False, f"neither parent is on {base}", False
        code, tree = self._git(root, ["rev-parse", "--verify", "--quiet", f"{sha}^{{tree}}"])
        if code != 0:
            return False, "no tree", False
        patterns = self.generated_paths
        objects = self._objects_dir(root)
        if objects is None:
            return False, "no object directory", True
        tmp = tempfile.mkdtemp(prefix="lazyagent-merge-")
        try:
            env = {"GIT_OBJECT_DIRECTORY": os.path.join(tmp, "objects"),
                   "GIT_ALTERNATE_OBJECT_DIRECTORIES": objects,
                   "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            os.mkdir(env["GIT_OBJECT_DIRECTORY"])
            merged, conflicted, why = self._auto_merge(root, parents[0], parents[1], env, tmp,
                                                       patterns)
            if merged is None:
                return False, why, True
            if merged == tree.strip() and not conflicted:
                return True, CLEAN, True
            if not patterns:
                return False, "the tree differs from the automatic merge", True
            code, out = self._git(root, ["diff-tree", "-r", "--no-renames", "--name-only", "-z",
                                         merged, tree.strip()], env)
            if code != 0:
                return False, "git diff-tree failed", True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        differ = set(filter(None, out.split("\0"))) | conflicted
        if differ and all(generated(p, patterns) for p in differ):
            return True, GENERATED, True
        return False, "the tree differs from the automatic merge", True

    def _auto_merge(self, root: str, p1: str, p2: str, env: dict[str, str], tmp: str,
                    patterns: tuple[str, ...] = ()) -> tuple[str | None, set[str], str]:
        """The tree of the automatic merge of ``p1`` and ``p2`` and its
        conflicted paths (only those ``patterns`` match: any other ends it);
        None, why."""
        if self.version >= (2, 38):
            args = ["merge-tree", "--write-tree", "--no-messages", "-z"]
            if patterns:
                args.append("--name-only")
            code, out = self._git(root, [*args, p1, p2], env)
            # The tree, then (on conflicts) each conflicted path, NUL-ended;
            # the tree's own end is a NUL or a newline by git version.
            cut = min((i for i in (out.find("\0"), out.find("\n")) if i >= 0), default=len(out))
            tree, rest = out[:cut].strip(), out[cut + 1:]
            if code == 1:
                conflicted = {p for p in rest.split("\0") if p.strip()}
                if not patterns or not tree or not conflicted \
                        or not all(generated(p, patterns) for p in conflicted):
                    return None, set(), "conflicts"
                return tree, conflicted, ""
            if code != 0 or not tree:
                return None, set(), "git merge-tree failed"
            return tree, set(), ""
        return self._index_merge(root, p1, p2, env, tmp, patterns)

    def _index_merge(self, root: str, p1: str, p2: str, env: dict[str, str],
                     tmp: str, patterns: tuple[str, ...] = ()
                     ) -> tuple[str | None, set[str], str]:
        code, out = self._git(root, ["merge-base", "--all", p1, p2])
        bases = out.split()
        if code != 0 or len(bases) != 1:
            return None, set(), f"{len(bases)} merge bases"
        if self._git(root, ["read-tree", "-i", "-m", "--aggressive", bases[0], p1, p2],
                     env)[0] != 0:
            return None, set(), "git read-tree failed"
        code, out = self._git(root, ["ls-files", "-u", "-z"], env)
        if code != 0:
            return None, set(), "git ls-files failed"
        stages: dict[str, dict[int, tuple[str, str]]] = {}
        for entry in filter(None, out.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, blob, stage = meta.split()
            stages.setdefault(path, {})[int(stage)] = (mode, blob)
        resolved, conflicted = [], set()
        for path, entry in stages.items():
            why = self._merge_path(root, path, entry, env, tmp, resolved)
            if why is None:
                continue
            if not generated(path, patterns):
                return None, set(), why
            # A generated file the merge cannot make: out of the index,
            # compared as differing.
            conflicted.add(path)
            resolved.append(f"0 {'0' * 40}\t{path}")
        if resolved and self._git(root, ["update-index", "--index-info"], env,
                                  stdin="\n".join(resolved) + "\n")[0] != 0:
            return None, set(), "git update-index failed"
        code, out = self._git(root, ["write-tree"], env)
        if code != 0 or not out.strip():
            return None, set(), "git write-tree failed"
        return out.strip(), conflicted, ""

    def _merge_path(self, root: str, path: str, entry: dict[int, tuple[str, str]],
                    env: dict[str, str], tmp: str, resolved: list[str]) -> str | None:
        """Merge one unmerged path into ``resolved``; why not, else None."""
        if set(entry) != {1, 2, 3}:
            return f"{path}: deleted or added on both sides"
        (m1, b1), (m2, b2), (m3, b3) = entry[1], entry[2], entry[3]
        mode = m2 if m2 == m3 else m3 if m1 == m2 else m2 if m1 == m3 else None
        if mode is None:
            return f"{path}: mode changed on both sides"
        if not {m1, m2, m3} <= {"100644", "100755"}:
            return f"{path}: a symlink or submodule changed on both sides"
        files = []
        for n, blob in enumerate((b2, b1, b3)):
            dest = os.path.join(tmp, f"blob{n}")
            code, _ = self._git(root, ["cat-file", "blob", blob], env, out_path=dest)
            if code != 0:
                return f"{path}: blob missing"
            files.append(dest)
        dest = os.path.join(tmp, "merged")
        code, _ = self._git(root, ["merge-file", "-p", *files], env, out_path=dest)
        if code != 0:
            return f"{path}: " + ("conflicts" if 0 < code < 128 else "cannot merge")
        code, blob = self._git(root, ["hash-object", "-w", "--no-filters", dest], env)
        if code != 0:
            return "git hash-object failed"
        resolved.append(f"{mode} {blob.strip()}\t{path}")
        return None

    # --- git ------------------------------------------------------------

    def _base_refs(self, root: str, base: str) -> list[str]:
        """``origin/<base>`` and ``<base>``, those that exist."""
        return [ref for ref in (f"refs/remotes/origin/{base}", f"refs/heads/{base}")
                if self._git(root, ["rev-parse", "--verify", "--quiet", ref])[0] == 0]

    def _have(self, root: str, sha: str) -> bool:
        return self._git(root, ["cat-file", "-e", f"{sha}^{{commit}}"])[0] == 0

    def _rev_list(self, root: str, tip: str, *exclude: str) -> list[str] | None:
        code, out = self._git(root, ["rev-list", f"--max-count={MAX_RANGE + 1}", tip,
                                     "--not", *exclude])
        return out.split() if code == 0 else None

    def _objects_dir(self, root: str) -> str | None:
        with self._lock:
            if root in self._objects:
                return self._objects[root]
        code, out = self._git(root, ["rev-parse", "--git-common-dir"])
        if code != 0 or not out.strip():
            return None
        path = str((Path(root) / out.strip() / "objects").resolve())
        with self._lock:
            self._objects[root] = path
        return path

    def _fetch_once(self, root: str, shas: tuple[str, ...], base: str) -> bool:
        """One fetch of ``shas`` and ``base`` from origin, unless one was
        tried for them within :data:`FETCH_EVERY`. True when it ran."""
        if not self._fetch or self.version < (2, 29):
            return False
        now = self._clock()
        with self._lock:
            if all(now - self._fetched.get((root, s), -FETCH_EVERY) < FETCH_EVERY
                   for s in shas):
                return False
            for s in shas:
                self._fetched[(root, s)] = now
        missing = [s for s in shas if not self._have(root, s)]
        code, _ = self._git(root, ["fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                                   "origin", *missing, f"refs/heads/{base}"],
                            timeout=_FETCH_TIMEOUT)
        diag.event(log, "base_merges.fetched", shas=[s[:10] for s in missing], base=base,
                   ok=code == 0)
        return True

    def _git(self, root: str, args: list[str], env: dict[str, str] | None = None, *,
             stdin: str | None = None, out_path: str | None = None,
             timeout: float = _GIT_TIMEOUT) -> tuple[int, str]:
        from lazyagent.worktree_manager import _run

        self.git_calls += 1
        full = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})}
        try:
            if out_path is not None:
                with open(out_path, "wb") as sink:
                    proc = _run(["git", *args], stdout=sink, stderr=subprocess.DEVNULL,
                                cwd=root, env=full, timeout=timeout)
                return proc.returncode, ""
            proc = _run(["git", *args], input=stdin, capture_output=True, text=True,
                        cwd=root, env=full, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return -1, ""
        return proc.returncode, proc.stdout or ""
