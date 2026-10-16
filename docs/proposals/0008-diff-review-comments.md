# 0008. Diff review with comments sent back to the agent

- **Status:** Proposed
- **Design refs:** [engine-split.md](../design/engine-split.md) §0b, §0e, §4d (`get_diff`, `send_agent_input`), §6 (Phase 3)

## Problem

- Reviewing an agent's work means reading the diff somewhere else (GitHub, editor) and copy-pasting feedback into the agent's terminal.

## Today's workaround

- Copy-paste. Line context is lost or retyped.

## Proposal

- The GUI shows the worktree diff (from `get_diff`, already in §4d), file by file, with inline comment threads, like a PR review but local and before any push.
- **Submit review** bundles the comments into one prompt (`path:line` + quoted hunk + comment) and sends it with `send_agent_input` to the worktree's agent.
- Threads are stored per worktree, so the next diff shows which comments the agent addressed (lines changed since the comment).

| Kind | Name | Shape / notes |
|---|---|---|
| method | `get_diff {worktree_path, base?}` | Structured: files → hunks → lines, not just text |
| method | `add_review_comment {worktree_path, path, line, body}` / `list_review_comments` / `resolve_review_comment` | Stored in the engine state dir |
| method | `submit_review {worktree_path, session_id?}` | Composes the prompt and calls `send_agent_input` |
| event | `review.updated` | `{worktree_path}` so several clients stay in sync |

## Fits after

- **Phase 3** in §6 ("Diff review with comments"), after a GUI exists: **H2** (host app) or the **web phase (PR 10+)**.
- This is the item where HTML pays off most (§0b): a diff viewer is free on the web, months of work in a terminal.

## Value / Effort

| Value | Effort |
|---|---|
| ★★ (needs the GUI) | M |

## Risks

- Large diffs: `get_diff` is capped today (worktree_manager.py:48-50); the GUI needs per-file lazy loading.
- Sending a long review as one prompt into a busy agent: must wait for `WAITING`, like [0002](0002-github-ci-triggers.md).

## Open questions

- Mirror comments to the GitHub PR once it exists, or keep local review strictly pre-push?
