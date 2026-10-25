# Home

The desktop app opens on **Home**: one line per work item, what needs you first.

Its guide is now **[How lazyagent works](how-lazyagent-works.md)**: the zones, the ball, every state, verb and action with its key, your rules and the facts, with screenshots. That page is generated from the rules catalog by `lazyagent rules doc --write`, so it never drifts from what the engine does; the same page is in the app (Help → How lazyagent works, `F1`; `?` on Home opens it at Home).

In short, since the second UX review: `a` approves and `⇧D` denies an agent's command (a deny waits 5 s for its Undo); `y` explains and `n` opens the notes on every line. The key bar names only the selected line's keys; `?` lists them all. A deep item under a quicker one (a changes requested under a red CI) also shows in To sit down with, and stale worktrees fold at the end of In flight with their uncommitted files.

The design is [proposal 0013](../proposals/0013-inbox-and-status.md) ([inbox-plan.md](../design/inbox-plan.md)) and, for the rules, [proposal 0014](../proposals/0014-rules-catalog.md) ([rules-plan.md](../design/rules-plan.md)).
