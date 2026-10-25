# 0014. Rules catalog: states, actions and the facts behind them

- **Status:** Accepted (2026-10-24; Q-R1-Q-R26 answered by the owner, all as recommended, with notes on Q-R7 and Q-R24). Plan in [rules-plan.md](../design/rules-plan.md) (R1 and R2 landed: its §5); the answers in "Open questions" below
- **Design refs:** [0013-inbox-and-status.md](0013-inbox-and-status.md) ("No LLM in the state path", "Kinds → level", "The orchestrator"); [inbox-plan.md](../design/inbox-plan.md) §3 (derivation), §5c (mandate, never alone), §7b (trackers), §8 I1-I8b as landed; [docs/guides/home.md](../guides/home.md)

Today the rules that decide every Home row live in four places that must agree by hand: `derive()` in `engine/status.py` (rules 1-16, first match wins), the kinds and detectors in `engine/actionables.py`, the verb table in the desktop (`home.rs` `verb()`), and the guide text (`home_help.rs` and `docs/guides/home.md`, kept in step only by a heading test). This proposal turns them into **one catalog**: a closed list of **facts** the engine computes, and **rules** over those facts that give a row its ball, state, level, verb and action. Today's behaviour becomes the built-in catalog, byte for byte. The user can override and add rules in TOML. The desktop gets a **"How lazyagent works"** page generated from the same catalog, with a **"Why is this row in this state?"** view. Evaluation stays deterministic: there is never an LLM at evaluation time. An LLM may only help the user *write* a rule, and the user accepts or rejects the result.

## Problem

- **The rules are code, scattered, and invisible.** A user who wants "Merge" to appear only when QA has passed must edit Python. Nobody can see which rule put a row where it is: the row carries a bare `rule: 4`, and the desktop maps `rule == 4` to "Merge" on its own.
- **The help drifts.** The Home guide is written by hand twice (Rust and Markdown). A test keeps the headings and keys in step, not the meaning: when a rule changes (I4d moved park before the queue, I7 made the CI item precise per head), the text must be updated by hand.
- **One team's definition of "ready" is not another's.** The owner's merge gate needs all of: the PR approved; CI green, or red only for reasons unrelated to the branch; the issue-tracker card marked "tested by QA" or "no QA required". Today's rule 4 knows only "approved, CI green, mergeable".
- **"CI red but not our fault" is common and nothing models it.** A shared unit-test check can be red on the base branch too; an eval suite whose pass rate varies run to run can be red with no regression at all. Today any red check puts the row in Round as "CI red", and the user or an agent re-checks the base by hand every time.

## Today's workaround

- Ask the orchestrator "is X really ready to merge?"; it reads the PR, the CI logs, the base branch's runs and the tracker card, and answers in prose. The answer is not recorded anywhere the row can use, and is lost at the next compaction.
- Label the row by hand ("others: QA") and remember to clear it.
- Read `status.py` to learn why a row is where it is.

## Proposal

### Concept

| Piece | Design |
|---|---|
| **Fact** | A named, typed value the engine computes about one work item: `pr.review`, `ci.state`, `ci.failing_tests`, `tracker.qa`, `agents.statuses`, … The list is **closed**: it is defined in code, with a source, a freshness and a cost for each fact. Rules can read facts; they cannot define new ones. A fact can be **unknown** (not linked, not fetched yet, not available) |
| **Rule** | `when <condition over facts> → then {ball, level, state, verb, action, item?}`. Conditions use a small expression language with no function definitions, no loops and no I/O |
| **Catalog** | The ordered rules plus the actions (keys, labels, engine methods) and the concepts text. The built-in catalog ships as package data (`lazyagent/engine/rules/builtin.toml`) and reproduces today's table exactly. User files override and add |
| **Evaluation** | The engine, on every status pass, deterministically: the same facts and the same catalog give the same row. No model, no network call in the evaluator; slow facts are fetched in the background and are unknown until they arrive |
| **Authoring** | The user writes a rule in plain language. An agent or the orchestrator translates it into TOML through the MCP tool `rules_propose`. The engine validates it and computes a preview of which current rows would change. The user accepts or rejects. Only then does the engine write the file |
| **Explanation** | `rules_explain {key}` returns the rule that matched, the facts it saw (value, source, age), and for each rule above it the first condition that failed. The desktop's "Why?" view and `lazyagent status --why <key>` show it |
| **Fixed in code** | The never-alone list, the orchestrator's mandate tables, the routing of tool approvals, the spawn caps and a few locked rules (merged and closed rows, tool approvals). Rules shape what the user **sees** and which user actions are **offered**. They never authorise an action by the orchestrator or an agent |

### Wireframe: the "Why?" view (desktop, `w` on a Home line)

```
┌ payments-retry #12 · why is this in ROUND · Merge? ────────────────────────────┐
│ Rule  merge  (your global rules.toml, line 31; replaces the built-in "merge")  │
│ Says  The PR is open, approved and mergeable; CI is green or red only for      │
│       reasons unrelated to the branch; QA tested it or it needs no QA.         │
│ Facts it saw                                         source          age       │
│   pr.state      = open                               PR poller       40s       │
│   pr.review     = approved                           PR poller       40s       │
│   pr.mergeable  = mergeable                          PR poller       40s       │
│   ci.state      = red                                PR checks       40s       │
│   ci.unrelated  = true                                                          │
│     unit-tests  red on base too: 2 of 2 failing tests also fail on base         │
│                 at 3f2a9c1 (merge-base)              JUnit artifact  6m         │
│     eval-suite  attested by agent a2 at 9be01d4: base 5 runs 71-78 %, branch   │
│                 3 runs 74-76 %; confirmed by you     attestation     22m        │
│   tracker.qa    = tested   (Jira status "QA passed") Jira adapter    3m         │
│ Rules above that did not match                                                  │
│   closed       pr.state is open, not merged                                     │
│   queue        no open inbox item                                               │
│   [o] open the rule file   [p] PR   [Esc] back                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

### Facts

Facts are grouped by namespace. **Cost** is the price of having the fact at all: *free* (already in the engine's memory), *poll* (comes with a poll the engine already runs), *extra* (a new call, made only when an active rule reads the fact). Facts of cost *extra* are **lazy**: if no rule in the effective catalog references them, the engine never fetches them.

| Fact | Type / values | Source | Freshness | Cost |
|---|---|---|---|---|
| `item.key`, `item.repo`, `item.branch` | text | worktree scan | on `worktrees.changed` | free |
| `item.is_main`, `item.is_merge_target` | bool | worktree scan, `merge_targets()` | same | free |
| `item.idle` | duration since the row's facts last changed | projection's `changed_at` | per pass (≤ 60 s) | free |
| `item.priority` | high · medium · low | feeder, tracker task (I6) | per pass | free |
| `item.project`, `item.waits_on` | text, count | feeders (I6) | per pass | free |
| `item.restorable` | count | restart manifest | `restorable.changed` | free |
| `agents.count`, `agents.running` | count | registry | `agent.*` events | free |
| `agents.statuses` | set of AgentStatus (+ `starting`) | registry | `agent.*` events | free |
| `agents.spawned_by_orchestrator` | bool | overlay `spawned` (I4d) | per pass | free |
| `pr.exists` | bool | PR poller | ≤ 60 s | poll |
| `pr.state` | open · merged · closed · none | `PrInfo.state` | ≤ 60 s | poll |
| `pr.review` | approved · changes_requested · review_required · none | `PrInfo.review_decision` | ≤ 60 s | poll |
| `pr.mergeable` | mergeable · conflicting · unknown | `PrInfo.mergeable` | ≤ 60 s | poll |
| `pr.review_requests`, `pr.reviews` | count or unknown | `PrInfo` (I4d) | ≤ 60 s | poll |
| `pr.number`, `pr.base`, `pr.head_sha` | int, text | `PrInfo` | ≤ 60 s | poll |
| `pr.merged_into` | text or none | `merged_into()` (I4d) | per head | free |
| `ci.state` | green · red · running · none | PR checks (`ci_state`) | ≤ 60 s | poll |
| `ci.head` | green · red · running · moved · none | Actions verdict on the head (I7) | ≤ `actions_poll_s` | poll |
| `ci.failing_checks` | set of check ids (`<workflow path>/<job name>`) | Actions jobs (I7), else PR check names | ≤ `actions_poll_s` | poll |
| `ci.first_failing` | text | same, first one | same | poll |
| `ci.failing_tests` | map check → set of test ids, or unknown per check | JUnit artifacts or log parsers (see "CI red but unrelated") | once per run attempt | extra |
| `ci.base_point` | sha or unknown | git + Actions run metadata (see below) | per head | extra |
| `ci.base_failing_tests` | map check → set of test ids, or unknown | the base branch's run at `ci.base_point` | once per base sha | extra |
| `ci.unrelated_checks` | set of check ids | derived from the two above (mechanism A) and accepted attestations (B) | per pass | extra |
| `ci.unrelated` | bool or unknown | every failing check is in `ci.unrelated_checks` | per pass | extra |
| `ci.attested` | set of check ids | accepted, unexpired attestations | per pass | free |
| `ci.related_failures` | map check → failing tests not explained (all of a check's failures when its test data is unknown) | failing checks minus `ci.unrelated_checks` | per pass | poll (extra when A or B is configured) |
| `ci.unrelated_summary` | text, e.g. `base also red: 3 tests`, `eval-suite attested by agent a2` | the evidence behind `ci.unrelated_checks` | per pass | free |
| `ci.base_fixed` | set of check ids | checks whose failures were all on the base at P, but that pass (or no longer fail those tests) on the base's newest run | ≤ `actions_poll_s` | extra |
| `ci.attestation_pending` | set of check ids | valid attestations waiting for the user's confirmation | per pass | free |
| `tracker.linked` | bool | tracker hub (I8a) or a feeder's `facts` | `poll_s` (300 s) | poll |
| `tracker.status` | text (raw status) | adapter | `poll_s` | poll |
| `tracker.state`, `tracker.ball` | text, ball | adapter mapping (I8a) | `poll_s` | poll |
| `tracker.qa` | tested · not_required · pending · failed · unknown | adapter `qa` mapping, else a feeder's `facts` | `poll_s` | poll |
| `tracker.stale` | bool | hub (last call failed) | `poll_s` | free |
| `items.count`, `items.top.kind`, `items.top.level`, `items.top.state`, `items.blocks_agent` | the work item's open inbox items | `ActionableQueue` | per pass | free |
| `gesture.kind`, `gesture.ball`, `gesture.text`, `gesture.until` | the user's label, park or close | overlay (I4a) | per pass | free |
| `item.kept`, `claim.active`, `hold.active`, `holder`, `delegation.phase` | first-responder state | claims, overlay (I3-I4d) | per pass | free |
| `report.needs_user`, `report.ball`, `report.note` | the agent's last `status_report` | overlay | per pass | free |
| `decisions.open`, `decisions.blocking` | count | decisions store | per pass | free |
| `orchestrator.alive`, `presence` | bool, present · away | registry, first responder | per pass | free |
| `now` | time | the engine's clock (passed in, so tests are deterministic) | per pass | free |

Notes:

- The fact registry is code (`engine/rules/facts.py`): name, type, allowed values, source, freshness, cost, a getter from today's `Facts`, and an English **phrase** per comparison ("the PR is approved"). The phrase is what the generated pages print, so the page cannot drift from the condition.
- Enum values are lower case in rules (`"approved"`), whatever the source spells (`APPROVED`). Comparing an enum fact to a value outside its list is a validation error ("`pr.review` is never `aproved`; did you mean `approved`?").
- A few helpers are facts with an argument, from a closed list: `agents.detail_of("<status>")` (the detail line of the first agent in that status), `ci.failing_tests_of("<check>")`.
- Feeders can set only `tracker.*` facts (see "Tracker QA").

### Rule language

**Shape.** A rule is a TOML table in an array `[[rule]]`:

```toml
[[rule]]
id = "merge"                     # unique; the same id as a built-in replaces it
says = "Merge when approved, CI is fine and QA is done."   # optional author's note
when = '''
pr.state == "open" and pr.review == "approved" and pr.mergeable == "mergeable"
'''
then = { ball = "you", level = "round", verb = "Merge", action = "open_pr",
         state = "merge: approved, CI green" }
```

| Key | Meaning |
|---|---|
| `id` | `[a-z0-9_.-]{1,48}`. Built-in ids are documented on the generated page (`closed.merged`, `queue`, `merge`, `changes_requested`, `conflict_no_agent`, `ci_red_no_agent`, `agent_finished`, `park`, `label`, `agent_running`, `ci_running`, `reported_ball`, `tracker_ball`, `no_reviewer`, `review_requested`, `agent_idle`, `restorable`, `no_activity`, `approved_not_mergeable`, `no_agent`, …) |
| `when` | A condition (below). Required |
| `and_when` | Only on an override of a built-in: a condition **added** to the built-in's own (`when` of the built-in `and` this). The easy way to make a built-in stricter without copying it |
| `then.ball` | you · agent · others · stalled · done |
| `then.level` | now · round · deep (only with ball `you`; default round). `deep` is the "To sit down with" zone. The zone follows: you → inbox, parked by the park rules only, else in flight |
| `then.state` / `then.state_expr` | A literal fact text, or a text expression from the closed function list: `with("CI red", ci.first_failing)` (the prefix alone when the second part is empty), `clip(text, n)`, `park_text(gesture.until)`, `waiting_text(...)`. Clipped to 5 words as today |
| `then.verb` | One word from the verb list (Approve, Answer, Merge, Review, Fix CI, Sync, Confirm, Assign, Decide, …; a user may add a verb word, plain text ≤ 12 characters) |
| `then.action` | The primary action's id from the action list (see "Actions"); it decides Enter and the key hint |
| `then.item` | Make the rule an **inbox item** (dismissable and parkable, with an occurrence, edge semantics like every kind). Default `true` for user rules with ball `you`, `false` otherwise. Built-in `merge` keeps `false` (byte-identical) |
| `then.blocks_agent`, `then.suggestion` | As today's row fields |
| `then.inherit` | `["ball"]` or `["state"]`: take that field from the rules **below** this one (today's park keeps the ball it would have; today's label keeps the state) |
| `before` / `after` | Place the rule relative to a non-locked rule id |
| `disabled` | `true` turns a non-locked built-in off |

**Conditions.** One expression, with:

| Kind | Syntax |
|---|---|
| Literals | `"text"`, `12`, `0.05`, `true`, `false`, durations `90s` `10m` `24h` `7d`, lists `["a", "b"]` |
| Comparison | `==` `!=` `<` `<=` `>` `>=` (numbers, durations, times) |
| Membership | `x in [..]`, `x not in [..]`, `"v" in set_fact`, `set_a subset set_b`, `empty(set)` |
| Unknown | `known(x)`, `unknown(x)` |
| Boolean | `not`, `and`, `or`, parentheses |
| Comments | `#` to the end of the line |

Precedence, highest first: parentheses, function call, comparison / membership, `not`, `and`, `or`. `a or b and c` is `a or (b and c)`. The validator warns on a mixed `and`/`or` without parentheses.

**Three-valued logic.** A comparison with an unknown fact is unknown. `and`, `or` and `not` follow Kleene's tables (`false and unknown` is false, `true or unknown` is true, `not unknown` is unknown). **A rule matches only when its condition is true.** Unknown never matches, so a missing fact cannot make "Merge" appear; the "Why?" view lists the unknown facts. `known()` lets a rule say what it wants when a fact is missing.

**Order and precedence.** Row rules are evaluated first match wins, in this order:

1. **Locked prefix** (code-fixed order, cannot be overridden, disabled or preceded): `closed.*` (merged, PR closed, already merged into a target, the close gesture), `park` (park gesture and a feeder's Waiting), `queue` (the work item's open inbox items, with approvals and questions Now), the first-responder rules (pass, hold, held brief, delegation).
2. **Repo rules** (`<repo>/.lazyagent/rules.toml`), in file order, unless placed with `before`/`after`.
3. **Global rules** (`$XDG_CONFIG_HOME/lazyagent/rules.toml`), in file order, unless placed.
4. **Built-in non-locked rules**, in today's order.

A rule with an id that already exists **replaces** the earlier definition in place (repo over global over built-in), keeping its position unless it says `before`/`after`. `and_when` on a built-in id narrows it in place.

**Item rules.** A rule with `then.item = true` is evaluated in the queue's pass, once per work item, and creates an inbox item `Rule:<rule id>:<work item>` while its condition holds, with the queue's usual occurrence, dismiss and park semantics. The row then reaches the inbox through the locked `queue` rule. Built-in kinds (approval, question, ci, ci_failed, review, conflict, done, …) are item rules too, with ids, levels and states as today; their detectors stay in code where they read events (exits, jobs), and their conditions and levels move to the catalog.

**Built-in catalog, abbreviated** (the full file is generated onto the "How lazyagent works" page):

```toml
[[rule]]
id = "merge"
when = '''pr.state == "open" and pr.review == "approved"
          and ci.state in ["green", "none"] and pr.mergeable == "mergeable"'''
then = { ball = "you", level = "round", verb = "Merge", action = "open_pr",
         state = "merge: approved, CI green", item = false }

[[rule]]
id = "agent_running"
when = '''agents.running > 0'''
then = { ball = "agent", state_expr = 'ci.state == "red" ? "fixing CI" : clip(agents.detail_of("running"), 5)' }
```

(The `? :` choice is the one extra operator, for text only, so today's rule 9 stays one rule.)

### The owner's merge gate, written generically

Global file, for every repo:

```toml
# $XDG_CONFIG_HOME/lazyagent/rules.toml
version = 1

[ci]
nondeterministic = ["eval-suite"]          # pass rate varies run to run (config, never code)
accept_attestation_for = ["eval-suite"]    # mechanism B is accepted for these checks only

[ci.check."unit-tests"]
tests_from = ["junit:junit-*", "log:pytest"]   # mechanism A: where failing-test ids come from

[[rule]]
id = "merge"                                   # replaces the built-in "merge"
says = "Merge when the PR is approved, CI is green or red only for reasons unrelated to the branch, and QA tested it or it needs none."
when = '''
pr.state == "open" and pr.review == "approved" and pr.mergeable == "mergeable"
and (ci.state in ["green", "none"] or ci.unrelated)
and tracker.qa in ["tested", "not_required"]
'''
then = { ball = "you", level = "round", verb = "Merge", action = "open_pr",
         state = "merge: approved, CI ok, QA ok" }

[[rule]]
id = "waiting_for_qa"
after = "merge"
when = '''
pr.state == "open" and pr.review == "approved"
and (ci.state in ["green", "none"] or ci.unrelated)
and tracker.qa not in ["tested", "not_required", "failed"]
'''
then = { ball = "others", state = "approved · waiting for QA" }

[[rule]]
id = "qa_failed"
after = "waiting_for_qa"
when = '''pr.state == "open" and tracker.qa == "failed"'''
then = { ball = "you", level = "round", verb = "Review", action = "open_tracker",
         state = "returned by QA" }
```

- The override has no `item` key, so as a user rule with ball `you` it becomes an inbox item: Merge can now be dismissed or parked (today's built-in merge cannot).
- `waiting_for_qa` matters: without it, an approved, green PR waiting for QA would fall through to the built-in tail and read "approved, not mergeable" (stalled). The preview in the authoring flow shows such fall-throughs ("2 rows would move from Merge to stalled: approved, not mergeable"), which is how the authoring agent learns to propose the companion rule.
- `tracker.qa not in [...]` is unknown when the row has no tracker card, so `waiting_for_qa` does not match a branch with no card, and neither does `merge`. A team where some branches have no card adds `or not tracker.linked` to the QA condition. The authoring agent asks this question back when it translates.

A per-repo file (`.lazyagent/rules.toml`) can then narrow it for one repo, for example `[[rule]] id = "merge" and_when = 'item.branch != "release"'`.

### CI red but unrelated to the branch

A red check counts as **unrelated** to the branch only through one of two mechanisms. Anything else is **related**, the conservative default.

#### A. Deterministic: the same failing tests on the base, at the same base point

**Rule.** For a failing check *c* on the head *H*:

```
unrelated_A(c)  ⇔  branch_failing(c, H) is known and non-empty
                 ∧ base_failing(c, P) is known
                 ∧ branch_failing(c, H) ⊆ base_failing(c, P)
```

where `branch_failing` and `base_failing` are **sets of failing test ids**, not check names, and *P* is the **base point** defined below. A unit-test check red on both sides but with a different set of failing tests is **not** unrelated: at least one failure is the branch's. A branch that fails a strict subset of the base's failures (it fixed some) is unrelated. `ci.unrelated` is true when every failing check is in `unrelated_A ∪ accepted attestations (B)`; one failing check with unknown data makes it unknown, so it never matches.

**Check identity.** A check is matched between the head's run and the base's run by **workflow file path and job name, matrix values included** (`.github/workflows/ci.yml/unit-tests (3.12)`), not by its display name, which can repeat or change. A job that exists only on the branch has no base data: unknown.

**Test identity.** `<check id>::<file or suite>::<class>::<name>`, parameters kept (`test_retry[exponential]`), durations, timestamps, colour codes and worker ids (`[gw3]`) stripped. Each source normalises to this form.

**The base point P.** Two definitions were asked for; both are precise, and they differ in when they are right.

| Definition | How it is computed | Right when | Wrong when |
|---|---|---|---|
| **M: merge-base** | `git merge-base H origin/<base>` (the newest commit both histories share). After a sync by merge or rebase it equals the base commit last synced in, so "last synced to" and merge-base are the same commit in the common cases | The failing run tested the branch's own head (a `push` event, or any run on the branch ref): that run contains exactly M plus the branch's commits | Criss-cross histories have several merge-bases (`git merge-base --all`); a rewritten base (force-push, revert of the synced commit) leaves M off the base's current history |
| **T: the base the run was built on** | For a `pull_request` run GitHub tests a **merge ref** (`refs/pull/<n>/merge`): the PR head merged into the base **tip at the time the run started**, not the commit the branch last synced to. T is that merge commit's base parent. The engine records it when it first sees the run: it fetches `refs/pull/<n>/merge`, checks that one parent is the run's `head_sha`, and keeps the other parent as T. If the ref has moved on (the base got a new commit, or GitHub rebuilt the ref) by the time the engine looks, T cannot be recovered and is unknown | Every `pull_request` run: failures the base tip brought in after the branch synced show up in the PR's CI even though the branch never contained them; comparing to M would call those failures the branch's | The engine first sees the run after the merge ref moved (engine down, long poll gaps): unknown |

**Decided (Q-R7):** by default P is **the base commit the failing run actually built on** (the *build base*): M for runs on the branch ref; T for merge-ref runs; unknown when it cannot be established (several merge-bases, a moved merge ref, a run event the engine does not know). Unknown means related.

**A configurable comparison base** (owner's note on Q-R7). In a project with stacked branches, a feature branch is often built on a project base branch (`feature-x-base`), itself built on the default branch. The project base may have no CI runs of its own, or runs whose failures are the project's own work in progress; what the team wants to know is whether the failure also exists on the default branch. So the comparison base is configurable, per repo and per project:

```toml
# rules.toml (global or repo)
[ci_unrelated]
compare_to = "build_base"          # default: P as defined above

[ci_unrelated.project."checkout-redesign"]   # an I6 project id
compare_to = "origin/main"         # any ref: compare against that branch's runs
```

- `compare_to = "build_base"` (default): P as above.
- `compare_to = "<ref>"` (e.g. `origin/main`): first compute the build base B (as above; unknown → the whole comparison is unknown). Then the comparison point is **the newest commit of `<ref>` that is an ancestor of B** (`git merge-base B <ref>`; with several merge-bases, unknown). The base data is that ref's own run of the same check at exactly that commit (a `push` run on the ref's branch with that `head_sha`).
- Lookup order: the project's `compare_to` (when the work item belongs to an I6 project), else the repo file's, else the global file's, else `build_base`.
- **Missing run:** no run of that check at exactly that commit (the ref batched its pushes, the run is older than the runs list the engine keeps, its artifacts or logs expired) → the base data is unknown → **related**, as everywhere else. Q-R8's answer (no nearest-run fallback) applies to a ref too.
- The "Why?" view names the comparison used: `unit-tests: compared with origin/main at 3f2a9c1 (project checkout-redesign)`.

**The base's failures at P.** The base branch's own run of the same check **at exactly P** (a `push` run on the base branch whose `head_sha == P`). Base branches often batch pushes, so P may have no run of its own; then the base data is **unknown** by default (Q-R8). The engine never starts a base run itself. An agent or the orchestrator may (a `ci_rerun` or `workflow_dispatch` is already an action they may take); its result is then picked up like any run.

**Where failing-test ids come from**, per check, in the order the config lists them (`tests_from`):

| Source | How | Limits |
|---|---|---|
| `junit:<artifact name glob>` | The run's artifacts (`GET /repos/{owner}/{repo}/actions/runs/{id}/artifacts`), the matching zip downloaded once, every `*.xml` parsed for `<testcase>` with `<failure>` or `<error>`. The most reliable source | The workflow must upload JUnit XML (`pytest --junitxml`, `jest-junit`, `go-junit-report`, `cargo nextest --junit`). Artifacts expire (90 days by default, often less), so old base runs lose them. Caps: 20 MB per zip, 200 files, 50 000 test cases; a hardened XML parser (no entities, no DTD) |
| `log:<parser>` | The failed log the engine already reads (`gh run view <id> --log-failed`, I7), split per job, given to a **built-in** parser: `pytest` (`FAILED <id> - …` summary lines), `jest` / `vitest` (`●`/`✕` test names under `FAIL <file>`), `go` (`--- FAIL: TestX (…)`, subtests kept), `cargo` (`test <path> ... FAILED`, the `failures:` list), `junit-console` (Maven/Gradle `Tests in error` / `FAILED` lines) | The log shows only failed steps, may be cut (the engine reads at most 30 lines per job for the summary; the parser reads the whole failed step, up to 5 MB), and changes format with tool versions. Each parser also reads the runner's own **totals** line (`3 failed, 120 passed`) and the result is used only when the number of ids it found equals the runner's failed count; otherwise unknown. A runner that retries flaky tests in-process reports them as passed, so they are invisible |
| `log:regex` | A user regex per check with a named group `test`, plus a `total` regex for the count check (Q-R10) | Same as above. Regexes are data: RE2 syntax only (no backreferences, no lookaround), compiled with `google-re2` when installed, else checked against that subset and run on lines cut to 300 characters |

**When test-level data is not available** (no artifact, no parser for the check, a count mismatch, a non-test failure such as lint or build, the base run's log already gone): `ci.failing_tests_of(c)` is unknown, `unrelated_A(c)` is false, the check is **related**. The "Why?" view says which step was missing ("unit-tests: no JUnit artifact; pytest parser found 1 of 3 failures").

**Cost.** Only when a rule in the effective catalog reads `ci.unrelated` (or a fact under it) **and** the head is red. One artifact list call plus one download (or one log read, already cached by I7) per failing job per run attempt; the same for the base run, cached per base sha, so many branches on the same base pay once. All calls go through the Actions hub's caps (20 `gh` calls a rolling minute, back-off on 429). The evaluator never waits: until the data arrives the fact is unknown and the row stays where today's rules put it.

#### B. Agent judgment for nondeterministic suites

An eval suite whose pass rate varies run to run (an LLM-graded suite, a benchmark with noise) cannot be compared by test ids: a test may fail on one run and pass on the next, on both sides. Deciding "the branch did not regress it" needs history and repetition, which is judgment. lazyagent models it as an **attestation**: a structured, signed-by-actor claim with evidence, which the user's rules may accept.

**Which checks.** Only checks listed in `[ci] nondeterministic` (config, never code). An attestation for any other check is refused: deterministic checks have mechanism A.

**What the agent must do before attesting** (the tool description says so; the engine checks what it can):

1. Read the suite's history on the base branch: at least `min_base_runs` (default 5) recent runs of that check, with their pass rates.
2. Run the suite on the branch head itself, `min_repetitions` times (default 3), and record the pass rate of each run.
3. Compare: the branch's pass rates must lie within the base's observed range minus a tolerance (`max_drop`, default 0.05 below the base's minimum), or the agent explains why the comparison it used is better.

**The attestation** (MCP `ci_attest`, the agent's or the orchestrator's token, for its own work item):

```jsonc
{
  "check": "eval-suite",                  // must be failing on the head, and listed as nondeterministic
  "verdict": "unrelated",
  "reason": "pass rate within the base's range over 5 base runs; failing cases differ every run",
  "sha": "9be01d4",                       // must equal the current head
  "base_runs": [                          // ≥ min_base_runs; run ids are checked against Actions
    {"run_id": 123456, "sha": "3f2a9c1", "passed": 71, "total": 100},
    ...
  ],
  "own_runs": [                           // ≥ min_repetitions, at "sha"
    {"passed": 74, "total": 100, "where": "local", "at": 1792830000},
    {"run_id": 123499, "passed": 76, "total": 100}
  ],
  "method": "range"                        // range | mean_ci | other (then explained in reason)
}
```

- **Validation.** The check is failing on the head and is nondeterministic; `sha` is the head; enough base and own runs; each `run_id` exists, is that check, and (for base runs) ran on the base branch; pass rates are numbers in range; with `method: "range"` the engine recomputes the comparison and refuses an attestation whose own numbers contradict it. Local runs cannot be verified: they are recorded as the actor's claim, with `where: "local"` shown.
- **Record.** In the overlay: `{id, key, check, sha, actor (agent:<id> | orchestrator), at, evidence, state: pending | accepted | rejected | expired | revoked, confirmed_by?}`, and a timeline entry "attested by agent a2".
- **Expiry.** When the head moves (any new commit), after `attestation_ttl` (72 h), and when the base point moves for that check. The user can revoke it from the "Why?" view.
- **Acceptance is the user's rule, per check.** `[ci] accept_attestation_for = ["eval-suite"]` lets an accepted attestation put that check in `ci.unrelated_checks`. A check not listed: the attestation is recorded and shown, but changes nothing.
- **Confirmation (Q-R12).** Per check, `confirm = "user"` (default) or `"none"`. With `user`, a valid attestation creates a Quick inbox item "Confirm: eval-suite failure unrelated?" with the evidence and two answers; it counts as accepted only once confirmed. Reason for the default: the agent that attests is usually the agent whose branch it is, so the claim is not independent. A team that trusts its agents' eval runs sets `none`.
- **The orchestrator** may attest only within its mandate: a new mandate action `attest_ci`, in the away list (allowed while away, proposed while present), shortened by `away_allow` like the others. The never-alone list is untouched: an accepted attestation can make "Merge" **appear**; merging stays the user's.

#### The Fix CI item when the failure turns out to be unrelated

Today (I4d delegation, I7 items) a delegated "Fix CI" item comes back to Round as `agent a2 finished; still CI red` whenever CI is still red, even when the agent has shown that the failure is not the branch's. Under 0014 the item follows the **related** failures, not the raw colour.

**Rules (built-in item rules, R4-R5):**

| Rule id | When | Then |
|---|---|---|
| `ci` / `ci_failed` (Fix CI) | `not empty(ci.related_failures)` and no agent running (today's other conditions unchanged) | Round, quick, verb **Fix CI**, action `delegate`; state names only the related checks: `CI red: unit-tests (2 new tests)` |
| `sync_base` (new) | `not empty(ci.base_fixed)` and `empty(ci.related_failures)` and no agent running | Round, quick, verb **Sync**, action `delegate` (the owner agent merges or rebases the base); state `Sync with base: unit-tests fixed on base` |
| `attestation_confirm` (new) | a check in `ci.attestation_pending` | Round, quick, verb **Confirm**: `agent says eval-suite failure is unrelated: accept?`, with the evidence and Accept / Reject (a comment) |

While an attestation is pending for a check, that check counts as related for `ci.related_failures` but the Fix CI item is held back while every related check is pending (the confirm item stands for it, as a brief stands for its agent's question).

**The chip.** When CI is red and every failing check is explained, the row keeps an informational chip instead of an inbox item: `CI red · unrelated (base also red: 3 tests)` or `CI red · unrelated (eval-suite attested by agent a2)`. Rules go on as if CI were acceptable, so the verb moves to the next rule that matches, e.g. **Merge** under the owner's gate. The chip is a new row field `ci_note` (opt-in through `status_list {fields}`), and the CLI table appends it to the state.

**Lifecycle, with the I4d delegation:**

1. CI turns red on head `9be01d4`: `unit-tests` (2 failing tests) and `eval-suite`. No test data yet → both related → **Fix CI** in Round.
2. The user presses `d`; the row reads `agent a2 · on it` (rule 16).
3. The agent looks. `unit-tests`: the engine's mechanism A has meanwhile read the JUnit artifacts: both failing tests also fail on the base at P = `3f2a9c1` → `unit-tests` unrelated. `eval-suite`: the agent reads 5 base runs, runs the suite 3 times on the head, and calls `ci_attest`.
4. `accept_attestation_for` lists `eval-suite` with `confirm = "user"` → **Confirm** item: "agent says eval-suite failure is unrelated: accept?". The delegation's fact (the Fix CI item) has cleared, so the delegation ends as `over`, with a timeline entry `agent a2: CI failures unrelated (base also red: 2 tests; eval-suite attested)`, not `still CI red`.
5. The user accepts. Every failing check is explained: no Fix CI, chip `CI red · unrelated (base also red: 2 tests; eval-suite attested by agent a2)`, and the row shows **Merge** if the PR is approved, mergeable and QA is done. With `confirm = "none"`, step 4 is skipped.
6. **Reject** instead: the attestation is `rejected`; the user's comment goes to the attesting agent through the mailbox (`Attestation <id> for eval-suite rejected by the user: <comment>`), and Fix CI comes back in Round as a new occurrence, state `CI red: eval-suite (attestation rejected)`, ready for `d` again (Q-R25).

**Re-triggers**, each a new occurrence of the item, with its reason in the state:

| Event | Effect | State shown |
|---|---|---|
| The head moves (a new commit) | Attestations for the old head expire; A is recomputed on the new head's run | `CI red: eval-suite (head moved, attestation expired)` until the new run's data says otherwise |
| A failing test not in the base set appears (a re-run, a new job) | That check leaves `ci.unrelated_checks` | `CI red: unit-tests (1 new failing test)` |
| The base gets fixed while the branch stays red (the base's newest run passes the tests the branch still fails) | Not Fix CI: the branch only lacks the base's fix | **Sync** `Sync with base: unit-tests fixed on base` |

**What Sync costs** (owner's note on Q-R24: only if it does not mean watching the base branch constantly). It adds no polling:

- **Runs:** I7 already reads the repo's latest 100 Actions runs with one ETag call per `actions_poll_s`, and the base branch's runs are in that list. `ci.base_fixed` first looks only at that list: the newest base run of the same check (at or after the comparison point) and its conclusion. A green newest base run of that check is enough; no further call.
- **Test-level data for the base** (only when the newest base run is still red, to see whether it still fails the branch's tests) is fetched lazily: only for rows currently in the unrelated state (every failing check explained by A), and cached per base run attempt, so one base run serves every branch on that base.
- **Cap:** at most 3 base-run test fetches per Actions poll (`[ci_unrelated] base_fetches_per_poll`, 3), inside the hub's 20 `gh` calls a minute; the rest wait for the next poll. Until fetched, `ci.base_fixed` is unknown and Sync does not fire.
- Base runs older than the 100 runs the list holds are not looked up: Sync stays unknown for them.

**Dismiss** stays per occurrence, as today: a dismissed Fix CI, Sync or Confirm item stays dismissed while its condition holds, and comes back on a re-trigger, which is a new occurrence.

**Byte-identical before A and B are configured.** With no `tests_from` for a check and no attestations, `ci.related_failures` is every failing check, so the `ci` rule fires exactly as today; `sync_base` and `attestation_confirm` never fire.

### Tracker QA

- `tracker.qa` is one of `tested · not_required · pending · failed · unknown`. Adapters map it from their own data in `trackers.toml`, next to the existing status mapping:

```toml
[jira.qa]                       # status name → qa value; unmapped → unknown
"QA passed"     = "tested"
"Ready to ship" = "tested"
"No QA"         = "not_required"
"In QA"         = "pending"
"QA failed"     = "failed"
# optional: a field instead of the status
# qa_field = "customfield_10042"
# qa_field_values = { "Tested" = "tested", "Not required" = "not_required" }

[github_issues.qa]              # labels → qa value
"qa-passed" = "tested"
"no-qa"     = "not_required"
"qa"        = "pending"
"qa-failed" = "failed"
```

- A repo's `.lazyagent/trackers.toml` may override `[<adapter>.qa]` as it may `[<adapter>.status]`; never credentials.
- **Feeders** (`work_items_upsert`) gain an optional `facts` object, so an external planner can push tracker facts for a tracker that has no adapter: `facts: {"tracker.qa": "tested", "tracker.status": "Ready to ship"}`. Only `tracker.*` facts from a fixed list are accepted (an unknown name or value is a validation error naming it). When an adapter and a feeder both give a fact for the same row, the adapter wins (it reads the source); the "Why?" view shows which one was used.

### Actions

The catalog also lists every user action with its key, so the generated page and the key hints come from one place. Actions are code (engine methods and desktop handlers); rules only choose which one is primary for a row (`then.action`) and the verb shown.

| Action id | Key | What it does |
|---|---|---|
| `answer` | Enter | Opens the agent's terminal at its dialog (approvals, questions) |
| `expand` | Enter | Expands the line (agents, PR, note, timeline) |
| `open_terminal` | o | The agent's terminal |
| `open_notes` | n | The agent's notes |
| `open_pr` | p | The PR (for Merge: merging is done there, by the user) |
| `open_tracker` | p | The tracker task |
| `dismiss` | x | Dismiss this occurrence of the item |
| `park` | z | Park the item or the work item (menu) |
| `label` | l | Put a ball and a text on the row |
| `close` | Shift+X | Close the work item (done / dropped) |
| `take_back` | u | Take back a pass, or clear your gesture |
| `pass` | Shift+O | Pass to orchestrator |
| `delegate` | d | Send the item to the branch's agent |
| `decision_view` | v | Open the brief |
| `explain` / `explain_restore` | y / Shift+Y | Explain this |
| `rebrief` | b | Request a new brief (decision view) |
| `seen` / `all_seen` | m / Shift+M | Mark seen |
| `revert` | Shift+U | Revert the orchestrator's decision |
| `why` | w | The "Why?" view (new) |
| `fold` / `fold_parked` | f / Shift+Z | Fold In flight / Parked |

Keys are fixed in this proposal (Q-R21).

### Authoring: plain language in, TOML out, the user decides

```
user ──"Merge only after QA"──▶ orchestrator (or an agent)
                                  │ rules_catalog  (facts, types, values, current rules)
                                  │ rules_propose {scope, toml, says}
                                  ▼
                               engine: validate → preview against current rows
                                  │ proposal p3 (pending)
                                  ▼
user ◀── Deep item "New rule: merge (replaces built-in)" ── TOML · sentence · preview diff
   [Accept] → engine writes the file (atomic), reloads; [Reject + note] → back to the proposer
```

1. **Ask.** The user types the rule in plain language in the "How lazyagent works" page (Rules tab, "Propose a rule…") or says it to the orchestrator. From the page, the engine sends the text through the first responder's mailbox to the orchestrator (or, with none running, offers to send it to an agent the user picks), as Explain this does. Nothing is sent without the user's click.
2. **Translate.** The agent reads `rules_catalog` (MCP: every fact with its type, values and phrase; every rule with its id and position; the actions and verbs) and calls `rules_propose {scope: "global" | "repo", toml, says, repo?}`.
3. **Validate and preview.** The engine runs the same validator as for a hand-written file, then evaluates **every current row** with the candidate catalog and with the current one, and returns the differences: rows whose zone, level, ball, verb or state would change, inbox items that would open or close, and warnings (a rule shadowed by one above it, a rule that matches nothing today, rows that would fall through to the built-in tail). An invalid proposal returns the errors to the agent, which may retry; nothing reaches the user.
4. **Decide.** A valid proposal is a Deep inbox item. The view shows the TOML (read-only), the generated English sentence (from the fact phrases, not the agent's words), the agent's `says`, and the preview diff. **Accept** makes the engine write the file (the global file, or the repo's `.lazyagent/rules.toml` in the main checkout, never committed by the engine), with a comment line `# proposed by <actor>, accepted by you, <date>`, and reload. **Reject** with a note sends the note back to the proposer.
5. **Hand edits** work the same way without step 1-3: the engine reloads a file when it changes (checked on its tick) and shows errors.

There is no LLM after step 2. The accepted TOML is evaluated by the engine like the built-ins.

### Files, precedence, validation and errors

| File | Who | Trust |
|---|---|---|
| Built-in (package data) | lazyagent | Always |
| `$XDG_CONFIG_HOME/lazyagent/rules.toml` | the user | Always (the user's own config) |
| `<repo>/.lazyagent/rules.toml` | anyone who commits to the repo | Loaded only once the user has **allowed** that file's content hash (`lazyagent rules allow`, or Allow on the banner); a changed file shows a banner with the same preview diff and stays on its last allowed version until allowed again (Q-R5) |

**Validation**, in order, per file: TOML parse (`tomllib`), schema (known keys, types, `version = 1`), each expression parsed and type-checked against the fact registry, then the catalog as a whole (duplicate ids in one file, `before`/`after` to an unknown or locked id, a cycle of placements, an override of a locked rule, an unknown verb or action, a fact with cost *extra* read by a rule in a repo with Actions off). Size caps: 256 KB per file, 200 rules, 2000 characters per expression, nesting depth 32.

**A bad rule falls back.** A rule that fails validation is skipped, alone. If it overrode a built-in (or a global rule), the overridden definition stays in force. Other rules in the file load. A file that does not parse at all is ignored as a whole, and the engine keeps that file's **last good** version (in memory, and as a copy under `state_dir()/rules/` so it survives a restart).

**Errors in the UI.** `rules.errors {errors: [{file, line, rule, message, fallback}]}` event, `rules_check {repo?}` method, `lazyagent rules check` CLI (exit 1 on errors). Home shows one line under the top bar while there are errors ("Rules: 1 error in .lazyagent/rules.toml — rule merge skipped, built-in in force · Show"), linking to the page's Rules tab, which lists each error at its file and line. Warnings (shadowed, matches nothing) are shown there only.

**Runtime.** An evaluation cannot fail on a valid rule: a missing fact is unknown and the rule does not match. A bug in a fact getter is caught per fact (the fact becomes unknown, the error is logged once per pass).

### The "How lazyagent works" page

- **Where.** The desktop's top menu gains **How lazyagent works** (under Help, and its own entry `F1`); `?` on Home opens it at the Home section. It **replaces** the hand-written Home guide (Q-R16).
- **What.** Generated, never hand-written, from `rules_catalog {repo?}`:
  - **Concepts**, short and schematic: engine (owns state, no LLM), agents, the orchestrator (a delegate with a mandate; never alone list), Home, the zones (Now, Round, To sit down with, In flight, Parked), the ball. This text lives in the catalog package (`concepts.toml`), next to the built-in rules, so the one source changes together.
  - **Every state, verb and action**: a table per zone with the rules that lead there (the generated sentence, the state text, the verb, the primary action and its key), the actions table with keys, and the inbox kinds with their levels and classes.
  - **Your rules**: which file each rule comes from, what it replaces, its errors and warnings; "Propose a rule…".
  - **Facts**: the list with source, freshness and cost, and which facts the effective catalog makes the engine fetch.
- **No drift.** `docs/guides/how-lazyagent-works.md` is generated from the built-in catalog by `lazyagent rules doc`, and a test fails when the committed file differs. `docs/guides/home.md` becomes a pointer to it. The desktop renders the engine's catalog at run time (so the user's own rules show); with an engine that lacks the `rules` feature it shows the copy generated at build time.
- **"Why is this row in this state?"** `w` on any Home line (or the button in the expanded line) opens the view in the wireframe: `rules_explain {key}` → `{rule {id, origin, file, line, says, sentence}, then, facts [{name, value, source, at, unknown?}], above [{id, failed: <first false or unknown condition>}], unrelated {per check: mechanism, detail}}`. Also `lazyagent status --why <key>` and the MCP tool `rules_explain`, so the orchestrator answers "why is X in Round?" from the engine instead of guessing.

### Protocol additions

| Kind | Name | Phase | Shape / notes |
|---|---|---|---|
| feature | `rules` | R2 | |
| method | `rules_catalog {repo?}` | R2 | `{facts, rules, actions, verbs, concepts, files, errors}` |
| method | `rules_explain {key}` | R2 | As above |
| method | `rules_check {repo?}` | R2 | `{errors, warnings}` |
| method | `rules_allow {repo, hash}` | R2 | The user's only |
| param | `status_list {fields?: ["rule_id", "verb", "action"]}` | R2 | Opt-in, so today's `status_list` output and its golden fixtures stay byte-identical |
| event | `rules.errors`, `rules.changed` | R2 | |
| method / MCP | `rules_propose {scope, toml, says, repo?}` → `{id, valid, errors, preview}`; `rules_decide {id, accept, note?}` (user) | R7 | |
| method / MCP | `ci_attest {…}`; `ci_attestation_revoke {id}` (user) | R5 | |
| field | `work_items_upsert.facts` | R3 | `tracker.*` only |
| config | `[ci_unrelated] compare_to, base_fetches_per_poll`, `[ci_unrelated.project."<id>"] compare_to` | R4 | In `rules.toml` |
| config | `[ci] nondeterministic, accept_attestation_for, attestation_ttl`, `[ci.check."<name>"] tests_from, parser options, confirm, min_base_runs, min_repetitions, max_drop` | R4, R5 | In `rules.toml` |
| config | `[<adapter>.qa]`, `qa_field`, `qa_field_values` | R3 | In `trackers.toml` |

### Security: rules are data

- **No code execution.** Rules are parsed into an AST and evaluated by a small interpreter: no `eval`, no imports, no attribute access, no user-defined functions, no loops. The function list is closed (`with`, `clip`, `park_text`, `waiting_text`, `known`, `unknown`, `empty`, `agents.detail_of`, `ci.failing_tests_of`). Evaluation is linear in the expression size.
- **No I/O from rules.** A rule cannot name a URL, a path or a command. Facts are fetched by engine code, and only facts an active rule reads.
- **Output is plain text**, clipped (5-word states, 12-character verbs), never markup.
- **Regexes** (log parsers only) use RE2 syntax, on cut lines, so no catastrophic backtracking.
- **Untrusted text stays data.** CI logs, PR text, tracker text and attestation reasons are shown as text and never parsed as rules. A CI log that says "this failure is unrelated" changes nothing: only test-id sets (A) or a validated attestation with evidence (B) can.
- **Repo files need the user's allow** (by content hash), so cloning a repo cannot change what the user sees.
- **Authoring through an LLM** can be steered by text the agent read (prompt injection). The defence is the gate: the proposal is validated, its effect is previewed on real rows, and only the user's Accept writes it.
- **Rules cannot loosen the fixed list** (below).

### What stays fixed in code

| Fixed | Why |
|---|---|
| **Never alone** (merge to a default or protected branch, publish, force-push a shared branch, delete unpushed work, close/reopen a PR) | Irreversible. No rule, no attestation, no config can let the orchestrator or an agent do these. A rule can make "Merge" appear to the user; that is all |
| The orchestrator's mandate tables (`AWAY_ACTIONS`, `PRESENT_ACTIONS`), the caps, routing | Config may only shorten them (`away_allow`), as today |
| Tool approvals are Now, the user's, never routed | An agent is blocked on a permission only the user may give |
| Locked rules: `closed.*`, `park`, `queue`, the first-responder rules | They express the user's own gestures and the engine's ownership; a rule above them could hide what the user did or what blocks an agent |
| Rounds (no reordering under the cursor), the fact list, the evaluator's semantics | Behaviour of the UI and of the language, not policy |

### Performance

- The catalog is compiled once per load (built-in at start, user files on change) into closures over fact getters.
- A status pass evaluates each row against at most ~50 rules; facts are memoised per row per pass. Budget: under 2 ms for 100 rows (a benchmark test in R1). Today's pass is already coalesced to 1/s.
- *Extra* facts are fetched only when an active rule reads them, in the background hubs (Actions, trackers), within their existing call caps. The evaluator never blocks on them.
- The row's `fingerprint` (its age) keeps today's inputs for built-in rules, so built-in ages do not move. When a user rule matches, its id and the facts it read are added to the fingerprint, so a change of `tracker.qa` or `ci.unrelated` moves the row's age.

### Migration of today's table

1. **R1: the same table, as data.** `builtin.toml` holds rules 1-16 and the 14 kinds, with today's numbers kept (`num = 4` for `merge`, so the row's `rule` field stays the same integer). `derive()` becomes a call into the evaluator. The old `derive()` stays in the test tree as the oracle: every existing golden case, plus 10 000 random `Facts` (seeded), must give identical `Derived`. Every golden fixture in `tests/protocol/fixtures/` and every test in `tests/engine/test_status.py`, `test_actionables.py` and the rest stays byte-identical. No new field appears in any output.
2. **R2:** user files load; the new fields are opt-in (`status_list {fields}`); `rules_explain`. Only then is the oracle code removed from the engine.
3. **R6:** the desktop reads verbs and actions from the engine (`fields: ["verb", "action"]`), keeps its own `verb()` as the fallback for older engines, and the hand-written guide goes away.

## Fits after

- **I4e** (`inbox-i4e-home-ux-fixes`, in progress), which touches `engine/status.py` and `home_view.rs`: R1 starts from `develop` once I4e has merged, so the oracle captures I4e's final table.
- I7 (Actions runs, failed-log reading) for mechanism A; I8a/I8b (adapters) and I6 (feeders) for tracker QA; I4a-I4b (overlay, mandate) for attestations.

## Value / Effort

| Value | Effort |
|---|---|
| ★★★ (the owner's merge gate; ends the help drift; "why?" answers the most common follow-up to a status table) | L: R1-R7 ≈ 45 h of agents, ≈ 11 h of owner review |

## Risks

| Risk | Mitigation |
|---|---|
| The migration changes a row somewhere | R1 keeps the old `derive()` as an oracle over all golden cases and 10 000 random facts; byte-identical fixtures |
| User rules make Home lie ("Merge" on a red PR) | Three-valued logic (unknown never matches); the preview before accept; the "Why?" view on every row; the locked prefix |
| A rule language grows into a programming language | Closed function list, no definitions or loops, size caps; new power comes as new facts in code |
| Mechanism A calls a real regression "unrelated" | Subset at test level, at the exact base point; count check on log parsers; unknown → related |
| Mechanism A is rarely available (no JUnit, logs cut) | Then it says so in "Why?"; JUnit upload is a one-line CI change the page suggests |
| An agent attests its own regression away | Evidence required and partly verified; per-check acceptance; user confirmation by default; expiry on head move; Merge stays never alone |
| Repo rules as an attack vector | Allow by content hash |
| Extra CI calls hit the rate limit | Lazy facts, per-run and per-base-sha caches, the Actions hub's caps and back-off |
| Generated page reads mechanically | The concepts text is written by hand once, in the catalog package; only the rule sentences are generated |

## Alternatives considered

| Alternative | Verdict |
|---|---|
| **An LLM at evaluation time** ("ask a model whether this row is ready to merge", "ask a model whether this failure is related") | **Rejected.** (1) Nondeterministic: the same facts could give a different row on the next pass, and rows would flicker; 0013's hard rule is that state has no LLM. (2) Cost and latency: passes run up to once a second over every row. (3) Untestable: no golden fixtures, no oracle. (4) Not explainable: "the model said so" is not a fact the "Why?" view can show. (5) Prompt injection: CI logs, PR descriptions and tracker comments are attacker-writable text, and a model reading them could be told to call a failure unrelated. (6) Offline and quota: Home would depend on a model API. Judgment is kept, but where it belongs: an agent does the work and files an attestation with evidence, which a deterministic rule accepts or not |
| Python plugins for rules (entry points, like trackers) | Rejected for user rules: code execution from a config file. Adding a **fact** stays a code change (or a tracker adapter) |
| An existing policy language (CEL, JSONLogic, Rego) | Considered. CEL is closest (typed, non-Turing-complete) but its Python implementations are heavy dependencies and it has no three-valued unknown; JSONLogic is unreadable in TOML; Rego is far too big. The grammar here is a small subset close to CEL's syntax, so a later switch is possible |
| Structured TOML conditions only (`when.pr.review = "approved"`) | Readable for one-line rules, but "or" and "not" become nested tables; rejected as the only form (Q-R1) |
| Keep the code table, add only config switches (`merge_requires_qa = true`) | Each team's gate needs a new switch and code; the help still drifts |
| Mechanism A at check level (red on base too → unrelated) | Rejected: a check red on both sides with different failing tests hides the branch's own failures, the case the owner raised |

## Open questions (answered 2026-10-24)

The owner answered all 26: the recommendation (column **Rec**) stands for every one. Q-R7 and Q-R24 carry an owner's note, applied in the text above.

| # | Question | Options | Rec |
|---|---|---|---|
| Q-R1 | Rule language | a) small expression grammar in a TOML string, three-valued; b) structured TOML tables only; c) a CEL library; d) JSONLogic | a |
| Q-R2 | Overriding a built-in rule | a) same `id` replaces it, plus `and_when` to narrow it; b) same `id` replaces only; c) no overrides, only new rules placed before | a |
| Q-R3 | Where user rules go by default | a) after the locked prefix, repo before global, before the built-ins; `before`/`after` to move; b) a numeric priority per rule; c) always after the built-ins | a |
| Q-R4 | Locked rules (cannot be overridden or preceded) | a) closed/merged, park, queue (approvals and questions), first-responder rules; b) only tool approvals; c) none | a |
| Q-R5 | Repo rules (`.lazyagent/rules.toml`) | a) loaded only after the user allows the file's hash; b) always loaded; c) no repo rules, global only | a |
| Q-R6 | Unknown facts | a) three-valued: unknown never matches, `known()` to test; b) unknown is false (so `not` makes it true); c) a default per fact | a |
| Q-R7 | Base point for mechanism A | a) the base the failing run built on: merge-base for branch runs, the merge ref's base parent for PR runs, else unknown; b) always the merge-base; c) always the base tip when the run started | aa — Owner's note: in projects with a base branch and dependent (stacked) branches it is sometimes better to compare against the default branch; a configurable comparison base `[ci_unrelated] compare_to` per repo and per project, default the build base (see "A configurable comparison base") |
| Q-R8 | No base run at exactly that point | a) unknown, so related; b) the nearest base run up to N commits before it, if every commit between touches no file under the check's paths; c) ask the agent to start a base run | a |
| Q-R9 | Sources of failing-test ids in R4 | a) JUnit artifacts plus built-in log parsers (pytest, jest/vitest, go, cargo, junit-console); b) JUnit artifacts only; c) log parsers only | a |
| Q-R10 | User regex parsers per check | a) allowed, RE2 syntax only, with a count regex; b) not in R4, built-in parsers only; c) allowed, Python `re` with line caps | a |
| Q-R11 | How an agent files an attestation | a) a dedicated MCP tool `ci_attest`; b) a field of `status_report`; c) a brief (`request_decision`) of a special kind | a |
| Q-R12 | Does an attestation need the user's confirmation? | a) per check `confirm`, default `user` (a Quick item); b) never: the rules decide; c) always | a |
| Q-R13 | The orchestrator attesting | a) new mandate action `attest_ci`: allowed away, proposed while present; b) never; c) like a worker, no mandate check | a |
| Q-R14 | When an attestation expires | a) head moves, base point moves, or 72 h; b) head moves only; c) head moves or a new failing run at the same head | a |
| Q-R15 | Feeder facts (`work_items_upsert.facts`) | a) `tracker.*` only, the adapter wins over a feeder; b) any namespace under `ext.*`; c) `tracker.*`, the feeder wins | a |
| Q-R16 | "How lazyagent works" and the Home guide | a) one generated page replaces the guide, `?` opens it at Home; b) keep both; c) generate only the rules part, keep the guide hand-written | a |
| Q-R17 | Where "Why?" shows | a) `w` on a Home line, the expanded line, `lazyagent status --why`, MCP `rules_explain`; b) the expanded line only; c) CLI and MCP only | a |
| Q-R18 | Who translates plain language into TOML | a) the orchestrator when running, else an agent the user picks, through `rules_propose`; b) the orchestrator only; c) a form builder in the desktop, no LLM | a |
| Q-R19 | Where the user accepts a proposal | a) a Deep inbox item with TOML, sentence and preview; b) a dialog in the How page only; c) the CLI only | a |
| Q-R20 | Exposing `rule_id`, `verb`, `action` on rows | a) opt-in `status_list {fields}`, fixtures untouched; b) only through `rules_explain`, the desktop keeps its verb table; c) always on, fixtures regenerated | a |
| Q-R21 | Rebinding keys | a) fixed in this proposal; b) in `rules.toml`; c) in the desktop settings | a |
| Q-R22 | Built-in `merge`: a dismissable inbox item? | a) stays row-only in R1 (byte-identical), becomes an item in R2 as a visible change; b) stays row-only; c) only user overrides are items | a |
| Q-R23 | How the "unrelated" chip is shown | a) a new opt-in row field `ci_note`, appended to the state in the CLI table; b) folded into the state text (≤ 5 words, so cut); c) only in the expanded line and "Why?" | a |
| Q-R24 | When "Sync with base" fires | a) the base's newest run no longer fails the tests the branch still fails; b) any newer green run of that check on the base; c) never: keep the chip, the user notices | aa — Owner's note: only if it does not mean watching the base branch constantly; it reuses I7's runs list (no extra polling), fetches base test data lazily for rows in the unrelated state, cached per base run, capped per poll (see "What Sync costs") |
| Q-R25 | After the user rejects an attestation | a) the comment goes to the attesting agent, Fix CI returns to Round for `d`; b) the same, and the item is re-delegated to that agent automatically; c) Fix CI returns, nothing is sent | a |
| Q-R26 | While an attestation waits for confirmation | a) the Confirm item replaces Fix CI for the checks it covers; b) both items show; c) Fix CI stays, Confirm shows in the expanded line | a |
