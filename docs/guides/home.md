# Home: a guide

The desktop app opens on **Home**. This is its guide; the same text is in the app (`?` on Home, or Help → Home guide). The design is [proposal 0013](../proposals/0013-inbox-and-status.md) and [inbox-plan.md](../design/inbox-plan.md).

## Keys

| Key | Action |
|---|---|
| `J K ↓ ↑` | Next / previous line |
| `Tab` | The next line that needs you (Now, Round, To sit down with) |
| `1 2 3 4` | Jump to Now, Round, To sit down with, In flight (on an expanded brief: pick an option) |
| `Enter` | Expand a line (an approval's command, a question's field); open a brief's decision view; without inline answers, a Now line's terminal |
| `Esc` | Close the open form, else collapse the expanded line |
| `Y  N` | On an approval line: approve / deny |
| `Ctrl+Enter` | On an expanded brief: answer with the picked option |
| `V` | The decision view of the line's brief (or of the orchestrator's decision) |
| `Y  ⇧Y` | Explain this: by its agent or the orchestrator / restore its agent and explain |
| `O` | The line's agent terminal |
| `N` | Its session notes |
| `P` | Its PR (or tracker task) |
| `X` | Dismiss the line's item (this occurrence, for every client); Undo for 5 s |
| `Z` | Park: 1 h, tomorrow 08:00, 1 week, until something changes, indefinitely (keys 1-5; W the whole work item); Undo for 5 s |
| `⇧Z` | Show / hide Parked |
| `⇧F` | Show / hide In flight's done rows ("8 done today") |
| `⇧O` | Pass to the orchestrator; Undo for 5 s |
| `U` | Take a pass or hold back, or clear your label, park, close or delegation |
| `D` | Delegate to its agent (the branch's agent); Undo for 5 s |
| `L` | Label who has the ball (e.g. others · QA) |
| `⇧X` | Close the work item and stop its agent, after a confirmation (never the worktree, branch or PR) |
| `M  ⇧M` | Mark the line seen / everything seen |
| `⇧U` | Revert the orchestrator's decision on the line, after a confirmation |
| `T` | The orchestrator's actions today |
| `Ctrl+Z` | Undo the last dismiss, park, delegate or pass (within 5 s) |
| `F` | Fold In flight |
| `G W  G H` | The worktrees / back to Home |
| `?` | This guide |

### Keys in the decision view

| Key | Action |
|---|---|
| `1-4` | Pick an option |
| `Ctrl+Enter` | Answer |
| `]  [` | Next / previous evidence tab |
| `Y  ⇧Y` | Explain this / restore its agent and explain |
| `B` | Request a new brief |
| `O` | The agent's terminal |
| `⇧U` | Revert the orchestrator's decision |
| `Esc` | Back to Home |

⇧ is Shift. Outside Home the app keeps the TUI's keys; `?` there lists them all.

## What Home is

One line per work item: a branch with its PR, its agents and its CI. The engine derives every line from facts (agent status, PR, CI, git), so nothing on Home is a board to keep up to date. The items that need you come first; everything else is In flight below.

## Colors

- Orange is you: the ball is yours (Round verbs, the you ball). Nothing else is orange.
- Yellow: Now (an agent is blocked on you), and parked items that still need you.
- Purple: To sit down with, and your own labels, parks and closes.
- Cyan: the orchestrator (what it holds, says and decided), and lines an agent works on.
- Blue: the selected line (its bar) and the • dot of a line that changed since you looked.
- Grey: facts and ages. A dimmed line is done, or waits for the next round.

## Reading a line

An In flight line reads: • ▴ cart-discount #12 agent working · 12m · tests pass; opening the PR 3m.

- • the line changed since you looked; ▴ high priority (▾ low).
- cart-discount #12: the branch and its PR, cut in the middle when long (the full name on hover).
- agent: who has the ball (the ball column, below).
- working · 12m · tests pass; opening the PR: the state. A working agent shows how long it has worked and its own last report, never its tool calls.
- 3m: how long the line has been like this. A stalled line ages from its last real activity (a commit, notes, a report), never from when the engine started.

An inbox line starts with what to do, always a verb: Approve, Answer, Decide, Fix CI, Address review, Resolve conflict, Request reviewer, Merge, Review, Relaunch.

## The zones

- Now: an agent is blocked on you (a tool approval, a question). Shown at once.
- Round: quick things that need you but block nobody (CI red with no agent on it, a conflict, a merge-ready PR, a PR with no reviewer, an item the orchestrator handed back). Delivered in rounds, see below; the header reads "3 round (+1 next)".
- To sit down with: things that need reading (a decision brief, changes requested, a finished branch to review). Shown at once, outside the rounds, against your limit (deep_max).
- In flight: everything whose ball is not yours, with who has it and since when. Done rows (merged, closed, worktree removed) stay a day, folded into one group at its end, "8 done today ▸" (a click or ⇧F opens it; Home remembers it per repo). A done row with a suggestion ("close PR #47: its head is in develop") stays in place, dimmed, and counts in the header. "N not started" in its header counts the tasks a planner or a tracker gave Home that have no branch yet (see Projects, below).
- Parked: work items you parked, with their inbox items, folded until you open it (⇧Z). A parked line says when it comes back, or why it waits, and "needs you" when one of its items is still yours; the Round header then shows "1 parked needs you ▸". Items a planner holds in its own Waiting lane wait here too, as "waiting (board)" or "waiting: &lt;reason&gt;".

## The ball

Who must move next. you: the line is in your inbox. orchestrator: the orchestrator holds it (you passed it, it holds it while it verifies, or a brief or question was routed to it first). agent: an agent has it, directly or handed to it by you or the orchestrator. others: a reviewer or a teammate (or your label says so). stalled: nobody runs and nothing moves (no agent, an agent to restore, no activity for a day). done: merged, closed, or already merged into develop or main (with a suggestion to close the PR).

## The states

The state is a short fact, never a guess: "approve: npm test", "CI red: tests", "conflict with base", "merge: approved, CI green", "review requested", "PR has no reviewer", "working · 12m", "orchestrator · looking", "orchestrator · verifying tests", "agent a1 · fixing CI red", "agent a1 finished; still CI red", "parked until Tue 08:00", "waiting (board)". A label you set shows before it: "QA · CI running"; an open dependency after it: "Edit · waits on Cart totals". A held line shows the last real step under it ("last step: CI green").

## Answering on the line

- An approval: Enter shows the whole command; y approves and n denies. It goes to the agent's permission prompt only while that prompt is on its screen; otherwise Home says so and o opens the terminal. Tool approvals are always yours, never the orchestrator's.
- A question: Enter opens a field under the line; type the answer and press Enter. It reaches the agent once its turn is over, never in the middle of one.
- A brief: expanded, 1-4 pick an option and Ctrl+Enter answers; Enter (or v) opens the decision view with the evidence.

## Undo and confirmations

x dismiss, z park, d delegate and ⇧O pass show an Undo for 5 seconds (the button, or Ctrl+Z). ⇧X and ⇧U ask first and say what will happen: ⇧X closes the work item and stops its agent, and never touches the worktree, the branch or the PR; ⇧U reverts the orchestrator's decision, tells the agent, stops what it spawned and resets the branch while nothing was pushed.

## Rounds, and changed since your last look

Now lines show at once. A line entering Round waits for a cut point (you dismiss, park, pass or take back an item; you come back from a terminal; or 30 minutes pass); until then it shows dimmed under the Round, marked "next round", and the header counts it ("+1 next"; "deliver now ▸" delivers it). Between cut points nothing moves under your cursor.

A dot (•) marks a line whose timeline changed since you last looked at it. Expanding a line shows "Since you looked" ("nothing new" when nothing happened) and marks it seen; m marks one line, ⇧M everything. On a first launch nothing has a dot: what you never looked at counts as seen then.

## The decision view

Enter (or v) on a brief opens it: the question and its options on the left, the recommendation preselected (or the orchestrator's suggestion when it handed the brief to you), what the agent tried, how reversible it is and what goes wrong if the choice is wrong, and a comment box. The evidence is on the right, one tab per piece: a diff hunk, a log's or a file's lines, a notes section, a link; the item's timeline is under it. "Outdated" means the branch moved since the brief.

Answer (Ctrl+Enter) sends "Decision d3: B — &lt;label&gt;. &lt;comment&gt;" to the agent once its turn is over, never in the middle of one, and adds it to the agent's notes (Decisions), so a restored agent still knows. A decision the orchestrator took shows as "decided by orchestrator" with Revert.

- Explain this (y): the owner agent explains in at most 10 lines. When it is not running but can be restored, the view offers "Restore agent and explain" (⇧Y); it is never restored on its own. With neither, the orchestrator explains; with nobody, the button is disabled and Open terminal and Pass to orchestrator are there instead. y works on any Home line too (but on an approval line y approves).
- Request a new brief (b): through the same chain, with a note; the agent answers with a new brief that replaces this one.

## The orchestrator strip

On top of Home: what the orchestrator says it is doing now (its last report), its queue (the items you passed and the ones it holds, with the time left), and today's actions. A click on Today (or t) lists them, each with Revert… when it can still be reverted. Presence is at its right: "Presence: auto (present)".

## Pass to orchestrator

⇧O passes the item to the orchestrator for two hours: the line reads "orchestrator · looking" in In flight. When it is done it releases the item with a note that stays on the row ("orchestrator: re-ran CI"); if it hands the item back, the item returns to Round with that note as its state. u takes a pass back. A pass that runs out comes back as "delegation lapsed".

The orchestrator can also hold an item itself while it verifies a worker's branch or waits on your merge ("orchestrator · &lt;note&gt;"), and the engine holds the finished work of the agents it spawned for an hour first. If it does not release them, they come to you as "orchestrator did not respond".

## What the orchestrator may do

- Presence: you count as present while you use the app (input in the last 15 minutes), as away otherwise; Presence on the strip sets it (auto, present, away).
- Mandate: while you are present it answers only small reversible questions and relays, restores agents or re-runs CI; while you are away it may also answer reversible and partly reversible briefs, rebase through the owner agent and spawn workers within the caps. A brief that is not reversible always comes to you. A passed item allows the away list.
- Never alone: merge to a default or protected branch, publish (tag, release, package), force-push a shared branch, delete a branch or worktree with unpushed work, close or reopen a PR. Those are always yours.
- Revert: a decision the orchestrator took shows on its row for a day; ⇧U reverts it, tells the agent your choice, stops what it spawned and resets the branch while nothing was pushed.
- Keep for me (the engine's keep_for_me): nothing on that item is routed to the orchestrator.

## Projects, priority and not started

A planner or a script can feed Home the tasks it tracks (work_items_upsert). A task with no branch yet counts in In flight's header ("3 not started"), together with the issues assigned to you in a tracker: work you were given that nobody has started. Once a branch carries its ticket key (SHOP-12-checkout), the task joins that line with its title and priority. In flight groups lines by project under a header ("▸ Checkout revamp · 3"); a line whose dependency is still open or in review reads "waits on &lt;title&gt;". High priority shows ▴ before the name and low ▾; priority orders lines inside a level, after anything that blocks an agent. A project that "continues while in review" lets the orchestrator start its next task once the dependencies are merged or in review: alone while you are away, as a proposal while you are present. u on a waiting line unparks it until the planner changes it.

## Delegate to its agent

d (Delegate to its agent) hands the line's item to the agent already on the branch (CI red, a conflict, a failed run; with CI, the failed log goes with it): the line reads "agent a1 · fixing &lt;fact&gt;" in In flight. If the agent asks you something, that question shows instead. If it finishes its turn and the fact still holds, the item comes back to Round as "agent finished; still &lt;fact&gt;".

## CI on the head

The engine follows the GitHub Actions runs of every branch you have a worktree for. Only a fresh red counts: a run on the branch's current head that failed or timed out. A red run on an older commit does not. A red PR reads "Fix CI" with the workflow and the failed job ("CI red · Tests / unit (step: Run pytest)"); a branch with no PR gets the same line. Expanding it shows the failed log: each failed job, its failing step and the last lines of its log. d (Delegate to its agent) hands it to the branch's agent with that summary. A green re-run, or a new push, clears the line on its own.

## Auto-delegation

Off by default. In Auto-delegation (top bar) you choose which stalled kinds the engine hands over on its own (a conflict with the base branch, an agent that died, CI red on the head) and to whom (the branch's owner agent or the orchestrator). It tries at most twice per item, never on main or a teammate's branch; after two failed tries the item comes to To sit down with with what was tried.

## Getting the most out of Home

- Work in rounds. Answer Now at once (Tab goes to the next line that needs you), then take the Round when it is delivered rather than reacting to every line as it appears.
- Park and label. Park what cannot move today (until tomorrow, until something changes) and label what waits on a person (others · QA): your inbox then holds only what you can act on.
- Pass routine items. A red CI, a rebase or a relay is the orchestrator's or the branch agent's work: ⇧O or d, and read its note when it comes back.
- Set your presence. Mark yourself away before a meeting so the orchestrator may decide more alone, and present when you want to see its proposals first.
- Sit down with the deep items in a focus block, not between two Now items.
