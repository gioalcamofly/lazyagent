---
name: release
description: Cut a lazyagent release — merge the chosen feature branches into develop, bump the version, write the CHANGELOG entry, merge develop into main, tag vX.Y.Z to trigger the PyPI publish workflow, and publish the GitHub release. Use when the user says "release", "cut a release", "ship X.Y.Z", "publish to PyPI", "deploy lazyagent", or asks to tag a new version.
---

# Release lazyagent

Ships a version end to end: feature branches → `develop` → `main` → tag → PyPI → GitHub release.

Args (optional): the feature branches to include, e.g. `/release feat-x fix-y`. With no args, the release ships whatever is already on `develop` plus any branches the user picks in step 1.

**Release branch is `main`** (not `master`). Development lands on `develop`.

## Facts about this repo

| Thing | Where |
|---|---|
| Version string | `pyproject.toml` → `version = "X.Y.Z"` |
| Release notes | `CHANGELOG.md` (Keep a Changelog, SemVer, newest section on top) |
| PyPI publish | `.github/workflows/publish.yml` — fires on pushing a `v*` tag, Trusted Publishing (OIDC), no token needed |
| CI | `.github/workflows/ci.yml` — runs on `main` pushes and PRs to `main`, pytest on Python 3.10/3.11/3.12 |

The tag is the deploy trigger. Nothing publishes until `git push origin vX.Y.Z` lands.

## Steps

### 1. Merge feature branches into `develop`

```bash
git fetch origin --prune
git checkout develop && git pull --ff-only origin develop
# candidates: branches with commits develop doesn't have
for b in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep -vxE 'main|develop'); do
  n=$(git rev-list --count develop.."$b"); [ "$n" -gt 0 ] && echo "$n  $b  $(git log -1 --format='%cr — %s' "$b")"
done
```

- **Branches named in the args** → merge those.
- **No args** → show the candidate table and ask which to include (`AskUserQuestion`, multiSelect). Never guess — many local branches are stale or WIP. Choosing none is valid: skip to step 2.
- Before merging each branch, prefer its remote copy if it has one and is ahead (`git rev-list --count <b>..origin/<b>`); a local branch behind its remote would ship stale code.

Merge each, one at a time, with the project convention (`--no-ff`, git's default message):

```bash
git merge --no-ff <branch>        # → "Merge branch '<branch>' into develop"
```

- **Conflict → stop.** `git merge --abort`, report the branch and conflicting files, and hand back to the user. Do not resolve release merges unattended.
- Stacked branches (one already merged into another) are fine — git skips commits develop already has.

After all merges, run the suite locally, since CI does not run on `develop`:

```bash
uv sync --group dev && uv run pytest
```

Red tests → stop and report; do not push. Green → `git push origin develop`.

### 2. Preflight — stop here if anything fails

```bash
git fetch origin --tags --prune
git status --porcelain                      # tracked changes must be clean
git rev-list --count origin/main..origin/develop   # must be > 0
git tag --sort=-v:refname | head -5
grep '^version' pyproject.toml
```

- Untracked scratch files (`*.md` notes, `scripts/`) are fine — the release only touches `pyproject.toml` and `CHANGELOG.md`. **Uncommitted changes to tracked files are not** — ask the user what to do with them.
- If `origin/main..origin/develop` is 0 commits, there is nothing to release. Say so and stop.
- Confirm CI is green on develop's head before merging:
  ```bash
  gh run list --branch develop --limit 3
  ```
  If CI never ran on develop (it only runs on `main` and PRs targeting it), note that and rely on the post-merge run in step 6.

### 3. Decide the version

Read the commits being released:

```bash
git log --oneline --no-merges origin/main..origin/develop
```

Bump per SemVer, based on those commits:
- any `feat:` → **minor** (0.6.0 → 0.7.0)
- only `fix:` / `perf:` / `chore:` → **patch** (0.6.0 → 0.6.1)
- a breaking change → **major**, but confirm with the user first

State the proposed version and the reason in one line, then proceed unless the user objects. If the commit mix is ambiguous, ask.

### 4. Bump + changelog on `develop`

```bash
git checkout develop && git pull --ff-only origin develop
```

Edit `pyproject.toml` → `version = "X.Y.Z"`.

Prepend a section to `CHANGELOG.md` directly under the intro block, in the existing house style:

```markdown
## [X.Y.Z] - YYYY-MM-DD

### Added
- User-facing sentence, present tense, no trailing period

### Fixed
- ...

### Performance
- ...
```

Rules that keep it consistent with the existing entries:
- Only include sections that have content (`Added` / `Changed` / `Fixed` / `Performance` / `Removed`).
- Write for a **user of the TUI**, not for a reader of the diff. Name the keybinding, the MCP tool, the flag — not the function that changed.
- Wrap around 80 columns, matching neighbouring entries.
- Squash noisy commits into one line each; drop pure-internal churn (test refactors, lint passes) unless it changed behaviour.
- Date is today, `YYYY-MM-DD`.

Commit and push:

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore: release X.Y.Z"
git push origin develop
```

### 5. Merge into main

`--no-ff` is the project convention — the merge commit is the release marker.

```bash
git checkout main && git pull --ff-only origin main
git merge --no-ff develop -m "chore: merge develop to main for X.Y.Z release"
git push origin main
```

If the merge conflicts, stop and hand it back to the user — do not resolve release merges unattended.

### 6. Wait for CI on main

```bash
gh run list --branch main --limit 1
gh run watch <run-id>
```

**Do not tag on a red CI run.** If it fails, stop, report the failure, and let the user decide (fix forward on develop, then re-merge).

### 7. Tag → triggers PyPI

```bash
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push origin vX.Y.Z
```

Confirm the publish workflow actually started and finished:

```bash
gh run list --workflow publish.yml --limit 1
gh run watch <run-id>
```

If the publish job fails, the tag already exists — **do not delete and re-push the same tag**. Fix the cause and cut the next patch version instead.

### 8. GitHub release

Notes = the new CHANGELOG section, reformatted for the release page: `##` headings (not `###`), one bullet per item, sentences ending in periods, bolded lead-in for the headline item. Close with the compare link.

Write the body to the scratchpad first, then:

```bash
gh release create vX.Y.Z \
  --target main \
  --title "vX.Y.Z" \
  --notes-file <scratchpad>/release-notes.md
```

Body shape (matches v0.6.0):

```markdown
## Added
- **Headline feature**: what it does and how to reach it.
- Secondary item.

## Fixed
- ...

**Full Changelog**: https://github.com/gioalcamofly/lazyagent/compare/vPREV...vX.Y.Z
```

### 9. Sync develop and report

```bash
git checkout develop && git merge --ff-only main && git push origin develop
```

Then report:
- version shipped
- PyPI workflow status + https://pypi.org/project/lazyagent/X.Y.Z/
- release URL
- anything skipped or degraded (e.g. CI not run on develop)

Finally, `mem_save` the release: version, date, headline changes, and any deviation from this procedure.

## Guardrails

- Never push a tag before CI on `main` is green.
- Never force-push `main` or `develop`, and never move an existing `vX.Y.Z` tag — PyPI rejects re-uploads of a version, so a bad tag means a new patch version.
- Never rewrite an existing CHANGELOG section; releases are append-only history.
- Steps 1 (push), 5, 7, and 8 are outward-facing and effectively irreversible. If this skill was invoked without an explicit "ship it", show the planned version and the drafted CHANGELOG entry and get a confirmation before step 5.
