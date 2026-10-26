---
name: lazyagent
description: How to work inside lazyagent, which runs coding agents in git worktrees and shows the user one inbox. Report status with status_report, ask for decisions with request_decision briefs, keep session notes, report back to an orchestrator, and explain a row with rules_explain. Use when lazyagent's MCP tools (server lazyagent_app or lazyagent) are available.
---
{{stamp}}
# Working inside lazyagent

lazyagent runs coding agents in git worktrees and shows the user one inbox
(Home): every branch, who has the ball, its state, and what needs the user.
The engine already sees commits, PRs, CI and your terminal status. Tell it
only what it cannot see.

This applies when lazyagent's MCP tools are there: the server is
`lazyagent_app` in an agent lazyagent started, `lazyagent` when the user
registered it by hand. `status_report` and `request_decision` need the agent
token lazyagent gives the agents it starts; in any other session they are
refused, so skip those parts there.

## Where the ball is: status_report

One line, when the ball moves and the engine cannot see it. No progress
chatter.

| Situation | Call |
|---|---|
| You wait on someone else (QA, a reviewer, a CI run elsewhere) | `status_report(note="waiting for QA", ball="others")` |
| You are blocked | `status_report(note="...", ball="stalled")` |
| The user must act | `status_report(note="what they must do", needs_user=true)` |
| You end your turn while work goes on in the background (a build, a watch, a subagent) | `status_report(note="...", ball="agent", until="45m")` |
| After an action outside the repo (tracker comment, PR, PR comment) | `status_report(outward={"kind": "pr_create", "ref": "<url>", "text": "..."})` |

`until` is a duration (at most 8 h; 2 h by default for `ball="agent"`) or a
Unix time. Past it, or once you end a later turn without a new report, the
engine reads your status again.

## A choice that needs reading: request_decision

Two designs, a trade-off, a risky or hard-to-undo step, what to do after your
tries failed, this branch or a separate task. Whenever you need the user to
choose, write a brief, then end your turn: a choice left only as prose at the
end of a turn reaches the inbox as a bare "Asks you" line, with no options or
evidence. Only a quick yes/no stays in the conversation.

```
request_decision(
  question="Retries: fixed or exponential backoff?",
  options=[{"key": "A", "label": "Exponential, cap 30 s", "consequence": "slower failure on outages"},
           {"key": "B", "label": "Fixed 2 s, 5 tries", "consequence": "3/30 runs time out"}],
  recommendation="A",
  tried="fixed 2 s: 3 of 30 runs hit the 10 s limit",
  evidence=[{"kind": "diff", "path": "src/retry.py", "hunk": "@@ -40,7 +40,12 @@"},
            {"kind": "log", "ref": "job:tests", "lines": "120-134"}],
  reversible="yes", impact_if_wrong="slower failures under load")
```

- Evidence kinds: `diff`, `log`, `file` (path, lines), `notes` (section), `url`.
  Paths are relative to your worktree. A brief with fewer than 2 options or no
  evidence becomes a quick question.
- At most 2 open briefs. `blocking=true` only when you cannot go on without
  the answer; then end your turn. Otherwise keep working on what does not
  depend on it.
- The answer arrives as `Decision <id>: <choice> ...`: follow it. Withdraw a
  brief you no longer need with `decision_withdraw(id)`.

## Session notes

`notes_read()`, `notes_append(section, text)`, `notes_replace_section(section, text)`.
Sections: Goal, Constraints (don't touch), Decisions, Doubts, Pending. They come
back to you after a compaction or a restore, so write there what the user
agreed or asked to keep, when it is set: the goal, a decision, a doubt, a step
added or done. Keep them short (8 KiB by default). In `manual` mode
(`notes_read`'s `mode`) write only when the user asks.

## Working under an orchestrator

When your instruction says an orchestrator started you, or names who to
report to:

- Report back with `SendMessage(to="<its session name>", message="...")`
  (Claude Code). Make the first line a self-contained sentence. A reply arrives
  as `<cross-session-message from="...">`; answer it with its `from` as `to`.
  Without SendMessage (Codex, Gemini), end your turn with the report: the
  orchestrator reads your screen and your notes.
- Optional convention, when your instruction asks for it: **ready to merge,
  then wait for the go.** When the work is done and the checks are green, send
  "ready to merge <branch>" with the commits, the test results, your decisions
  and your doubts, then wait. Do not merge, push or publish before the "go".

## If you are the orchestrator

lazyagent's own orchestrator gets a full prompt from lazyagent; this is the
short form, for an orchestrator or planner skill of your own.

- For status, call `status_list` once and show its `table`. Never build your
  own table from `list_worktrees` or agent output: it goes stale.
- `orchestrator_hold(key, note)` while you verify a worker's finished branch
  or wait on a merge: the row reads "orchestrator · <note>" instead of landing
  in the user's Round. End it with `orchestrator_release(key, "done", note)`, or
  `"returned"` with what the user must decide. A hold widens nothing.
- When a worker brings you a choice that needs reading, tell it to write a
  brief. Answer only within your mandate; escalate the rest
  (`decision_escalate`). Merging to the default branch, publishing,
  force-pushing, deleting unpushed work and closing PRs are never yours alone.

## Why a row is in its state: rules

Every Home row comes from rules over facts (`pr.review`, `ci.state`,
`agents.running`, `tracker.qa`, ...): the first match wins, and an unknown fact
never matches. The built-in catalog applies, then the user's
`~/.config/lazyagent/rules.toml` and the repo's `.lazyagent/rules.toml` (once
the user allows it) can replace, narrow, add, move or turn off rules.

- "Why is X in Round?", "why no Merge on X?": call `rules_explain(key)` and
  answer from its `text`. Do not guess from the PR or the logs. The user can
  run `lazyagent status --why <branch>`, or press `w` (Why?) on Home.
- Before helping with a rule, read `rules_catalog()`. Never write a rules file
  yourself: propose the rule; the user writes it or accepts it, and
  `lazyagent rules check` validates it.
