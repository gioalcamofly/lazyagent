# Home

The desktop app opens on **Home**: one line per work item, what needs you first.

Its guide is now **[How lazyagent works](how-lazyagent-works.md)**: the zones, the ball, every state, verb and action with its key, your rules and the facts, with screenshots. That page is generated from the rules catalog by `lazyagent rules doc --write`, so it never drifts from what the engine does; the same page is in the app (Help → How lazyagent works, `F1`; `?` on Home opens it at Home).

In short, since the second UX review: `a` approves and `⇧D` denies an agent's command (a deny waits 5 s for its Undo); `y` explains and `n` opens the notes on every line. The key bar names only the selected line's keys; `?` lists them all. A deep item under a quicker one (a changes requested under a red CI) also shows in To sit down with, and stale worktrees fold at the end of In flight with their uncommitted files. Since I4g that deep item is a line of its own there (`↳`): `j`/`k`, Tab and `3` reach it, Enter expands it on that item (a brief: pick with 1-4, answer with Ctrl+Enter, `v` for the decision view), and in any expanded line a brief behind the top one has **Answer here**. A red CI made only of checks on `[inbox] auto_delegate_skip_checks` is not auto-delegated; its line says so.

A teammate's PR that asks you (or a team of yours) for a review is a Round line of its own, with no worktree needed: **Review** · "review requested by <author> · <title>". `p` opens the PR; expanded, it says when you were asked and through which team. It goes once you review (approve, request changes or comment), the request is withdrawn or the PR closes; dismissed or parked, it reads "review requested · set aside" in In flight. It is never delegated. A teammate's branch you checked out reads as theirs ("ana's PR"), not as yours to merge or to find a reviewer for.

The design is [proposal 0013](../proposals/0013-inbox-and-status.md) ([inbox-plan.md](../design/inbox-plan.md)) and, for the rules, [proposal 0014](../proposals/0014-rules-catalog.md) ([rules-plan.md](../design/rules-plan.md)).
