# Rules catalog: implementation plan

- **Status:** Accepted (2026-10-24; Q-R1-Q-R26 answered, all as recommended, notes on Q-R7 and Q-R24). Each phase gets an "As landed" note in §5 when it merges.
- **Proposal:** [0014-rules-catalog.md](../proposals/0014-rules-catalog.md). This plan does not repeat its rationale. It covers only what to build, where, and how to prove it. Open questions Q-R1-Q-R26 and their answers are in the proposal; the phases below follow them.
- **Base:** `develop` **after I4e** (`inbox-i4e-home-ux-fixes`, in progress, touches `engine/status.py` and `desktop/crates/app/src/home_view.rs`). R1 must not start before I4e has merged: R1's oracle is the final §3 table.
- **Hard rule:** no LLM at evaluation time, in any phase. The only LLM step is authoring (R7), and its output is validated, previewed and accepted by the user before the engine writes it.
- **Byte-identical:** every golden fixture in `tests/protocol/fixtures/` and every existing engine test stays unchanged through R1-R5 unless a phase says otherwise. New output fields are opt-in (`status_list {fields}`).

## TL;DR

| # | Branch | Scope | One-line acceptance |
|---|---|---|---|
| R0 | `docs-rules-catalog` | Proposal 0014 + this plan | Merged |
| R1 | `rules-r1-catalog-evaluator` | Fact registry (closed list, types, values, phrases, source/freshness/cost); expression parser and three-valued evaluator; `builtin.toml` with rules 1-16 and the 14 kinds (today's numbers kept); `derive()` and the kinds' conditions/levels call the evaluator; old `derive()` kept in tests as the oracle | All existing tests and golden fixtures byte-identical; old vs new `derive()` identical on every golden case and 10 000 seeded random `Facts`; a pass over 100 rows evaluates in < 2 ms |
| R2 | `rules-r2-user-rules` | Global and repo `rules.toml`; precedence (locked prefix → repo → global → built-ins; `before`/`after`; same id replaces; `and_when`); validation and per-rule fallback, last-good file copy; repo allow by hash; `rules_catalog`, `rules_explain`, `rules_check`, `rules_allow`; `status_list {fields}`; events `rules.errors`, `rules.changed`; CLI `lazyagent rules check|show|allow|doc`, `lazyagent status --why`; MCP `rules_catalog`, `rules_explain`; built-in merge becomes an item (Q-R22); oracle code removed from the engine | A global rule that requires a fact makes Merge disappear from a matching row and `--why` names the rule, its file and line and the facts; a rule with an unknown fact is skipped, the built-in stays in force and Home's error line names it; a changed repo file is not applied until allowed |
| R3 | `rules-r3-tracker-qa` | `tracker.qa` in the hub; `[<adapter>.qa]`, `qa_field`, `qa_field_values` in `trackers.toml` (Jira, GitHub Issues); `work_items_upsert.facts` (`tracker.*` only; adapter wins) | Against the Jira fixtures, a status mapped to `tested` makes the owner's merge rule match; a feeder's `facts: {"tracker.qa": "not_required"}` does the same for a row with no adapter; with no `[*.qa]` the output is byte-identical |
| R4 | `rules-r4-ci-unrelated` | Mechanism A: check identity (workflow path + job, matrix); failing-test ids from JUnit artifacts and built-in log parsers (pytest, jest/vitest, go, cargo, junit-console) with the count check; optional RE2 user parsers; base point (merge-base for branch runs, merge-ref base parent for PR runs, else unknown); configurable comparison base `[ci_unrelated] compare_to` per repo and per I6 project (default the build base; a ref = its run at the newest commit of the ref that is an ancestor of the build base, missing → unknown); base failures at exactly P, cached per base sha; facts `ci.failing_tests`, `ci.base_point`, `ci.base_failing_tests`, `ci.unrelated_checks`, `ci.related_failures`, `ci.unrelated`, `ci.unrelated_summary`, `ci.base_fixed`; `ci`/`ci_failed` fire on related failures only; new kind `sync_base` (no extra polling: I7's runs list, base test data lazily for rows in the unrelated state, cached per base run, `base_fetches_per_poll` 3); row field `ci_note` (opt-in); lazy fetching | A unit-test check red on base and branch with the same two failing tests clears Fix CI and shows `CI red · unrelated (base also red: 2 tests)`; one extra failing test on the branch brings Fix CI back naming it; no JUnit and no parser keeps Fix CI (related) and `--why` says what was missing; the base fixed while the branch stays red gives Sync, not Fix CI, with no Actions call beyond the poll's own when the newest base run is green; with `compare_to = "origin/main"` for a project, a stacked branch is compared with main's run at the ancestor commit, and a missing run keeps Fix CI; nothing configured → byte-identical |
| R5 | `rules-r5-attestations` | Mechanism B: `[ci] nondeterministic`, `accept_attestation_for`, `attestation_ttl`, per-check `confirm`, `min_base_runs`, `min_repetitions`, `max_drop`; MCP/method `ci_attest` with validation (run ids checked against Actions, `range` recomputed); overlay store, expiry (head moves, base point moves, TTL), revoke; mandate action `attest_ci` (away list); `attestation_confirm` kind (Accept records, Reject sends the comment to the agent and brings Fix CI back); delegation ends `over` when the related set empties | The I4d worked example of the proposal runs end to end in a test: delegated Fix CI, A clears unit-tests, the agent attests eval-suite, Confirm item, accept → chip and Merge; reject → Fix CI back and the comment delivered; a new commit expires the attestation; an attestation for a check not listed as nondeterministic is refused |
| R6 | `rules-r6-how-it-works` | Desktop "How lazyagent works" (Help menu, F1, `?` on Home opens it at Home): concepts, zones, ball, every state, verb and action with its key, your rules, facts, errors; generated from `rules_catalog` (build-time copy for older engines); "Why?" view (`w`, expanded line); verbs and actions from `status_list {fields}` with `verb()` kept as fallback; `home_help.rs` replaced; `docs/guides/how-lazyagent-works.md` generated by `lazyagent rules doc` and checked by a test; `docs/guides/home.md` points to it | Changing a built-in rule's state text changes the page and the generated Markdown with no hand edit (the test fails until `rules doc` is re-run); `w` on a Merge line shows the rule, the facts and the rules above that failed |
| R7 | `rules-r7-authoring` | `rules_propose` (MCP; validate, preview on every current row: zone/level/ball/verb/state changes, items opened/closed, shadowed / matches-nothing / fall-through warnings); `rules_decide` (user); the proposal as a Deep inbox item (TOML, generated sentence, `says`, preview); "Propose a rule…" on the page sends the text to the orchestrator (or a picked agent) through the mailbox; the engine writes the file on Accept only; orchestrator prompt note | The orchestrator turns "Merge only when QA passed or isn't needed" into a valid proposal whose preview lists the rows that change and the fall-through warning; Accept writes the global file and the rows change on the next pass; Reject sends the note back; an invalid TOML never reaches the user |

Order: R1 → R2 → R6 ∥ R3 → R4 → R5 → R7. R6 needs only R2. R3 and R4 are independent; R5 needs R4's facts. R7 needs R2 and R6 (the page hosts "Propose a rule…").

Each phase is its own branch from up-to-date `develop`, merged with `--no-ff` once `uv run pytest` and (for desktop phases) `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings` and `cargo test` are green. Every phase updates the docs it changes (this plan §5, the proposal's status, README config tables, the generated guide from R6 on).

**Owner review time:** R1 ~2 h (the oracle and the catalog file are the review), R2 ~2 h, R3 ~45 min, R4 ~2 h, R5 ~1.5 h, R6 ~1.5 h, R7 ~1.5 h. ≈ 11 h. Agents implement (≈ 45 h).

---

## 1. Ground truth (what the code does today)

| Fact | Where | Consequence |
|---|---|---|
| Row derivation is `derive()` / `_derive_tail()`, first match wins, rule numbers 1-16 (13-16 are claims, held, hold, delegation) | `engine/status.py` `derive` | R1 encodes it as `builtin.toml`; `rule` stays the integer |
| Park and label reuse a sub-derivation (`derive(replace(facts, gesture=None))`, `_derive_tail`) | same | The `inherit` key (`["ball"]`, `["state"]`) |
| Rules 3, 5, 6 have a "no queue" branch (`queue is None`) | same | The `items.*` facts plus `queue.enabled`; the branch stays for `derive` without a queue |
| Inbox kinds: `register_kind` + detectors over a `Snapshot`, fixed levels and classes | `engine/actionables.py` | Conditions and levels move to item rules; event-fed detectors (exits, jobs) stay in code |
| The verb per line is a desktop table (`rule == 4` → Merge, kind → verb) | `desktop/crates/app/src/home.rs` `verb()` | R6 reads `verb` from the engine; the table stays as fallback |
| The guide is hand-written twice, kept in step by a heading/key test | `home_help.rs`, `docs/guides/home.md` | R6 generates it |
| Actions: fresh red on head, failed jobs, `--log-failed` parsing per job | `engine/actions.py` | R4 reuses the poll, the caches and the call caps; adds artifacts and base runs |
| Trackers: status → `{ball, state, level}` mapping, repo override of mappings only | `engine/trackers/__init__.py` | R3 adds `[<adapter>.qa]` beside `[<adapter>.status]` |
| Feeders: `work_items_upsert` replaces the record, validated fields | `engine/work_items.py` | R3 adds `facts` |
| Mandate tables and `NEVER_ALONE` are code; config only shortens | `engine/orchestrator.py` | R5 adds `attest_ci` to `AWAY_ACTIONS`; never alone untouched |
| Delegation returns `agent <id> finished; still <fact>` while the item id holds | `engine/delegation.py` | R4/R5: the `ci` item clears when the related set empties, so the delegation ends `over` |

## 2. Modules

```
engine/rules/
  __init__.py      # load, merge, compile; RulesHub (files, hashes, last good, errors)
  facts.py         # the fact registry: name, type, values, phrase, source, freshness, cost, getter
  lang.py          # tokenizer, Pratt parser, AST, type check, three-valued evaluator, text functions
  catalog.py       # Rule, ItemRule, Action, Verb; order, placement, locked prefix; explain trace
  builtin.toml     # today's table, as data
  concepts.toml    # the page's hand-written concepts text
  doc.py           # Markdown generation (lazyagent rules doc) and English sentences from phrases
  preview.py       # R7: evaluate rows under a candidate catalog, diff
engine/ci_tests.py # R4: JUnit and log parsers, test ids, base point, base cache
engine/attest.py   # R5: attestations, validation, expiry
```

## 3. Files to touch

| Phase | Files |
|---|---|
| R1 | `engine/rules/{__init__,facts,lang,catalog}.py`, `engine/rules/builtin.toml` (package data in `pyproject.toml`), `engine/status.py` (`derive` delegates; `Facts` unchanged), `engine/actionables.py` (kind conditions and levels from the catalog), `tests/engine/rules/test_lang.py`, `test_oracle.py` (old `derive` copied to `tests/engine/rules/oracle.py`; golden cases and seeded random facts), `test_bench.py` |
| R2 | `engine/rules/__init__.py` (files, precedence, allow by hash, last good under `state_dir()/rules/`), `engine/methods.py`, `engine/server.py` (`FEATURES`), `engine/events.py`, `engine/status.py` (`fields`, fingerprint inputs for user rules), `mcp_server.py`, `cli.py` (`rules`, `status --why`), `config.py`, `protocol/schema/v1/*`, new golden fixtures only for new methods, README |
| R3 | `engine/trackers/__init__.py` (`qa` mapping, `TrackerFact.qa`), `trackers/jira.py` (`qa_field`), `trackers/github_issues.py`, `engine/work_items.py` (`facts`), `engine/rules/facts.py`, schemas, `tests/engine/trackers/*`, `tests/engine/test_work_items.py` |
| R4 | `engine/ci_tests.py`, `engine/actions.py` (artifacts list/download, base runs, merge-ref base parent), `engine/actionables.py` (`ci`/`ci_failed` on related failures, `sync_base`), `engine/rules/facts.py`, `engine/status.py` (`ci_note`), `engine/delegation.py` (over when the item clears for "unrelated"), `tests/engine/test_ci_tests.py` (fixtures: JUnit XML, pytest/jest/go/cargo logs with totals, a truncated log, a matrix job), schemas |
| R5 | `engine/attest.py`, `engine/overlay.py` (store), `engine/orchestrator.py` (`attest_ci`), `engine/actionables.py` (`attestation_confirm`), `mcp_server.py` (`ci_attest`, when-to-use with the required evidence), `orchestrator_prompt.py`, `worker_prompt.py` (one line), schemas, `tests/engine/test_attest.py` (the proposal's worked example end to end) |
| R6 | desktop: `app.rs` (menu entry, F1), `how_it_works.rs` (new, replaces `home_help.rs`), `why_view.rs` (new), `home.rs` (`verb` from the row, fallback kept), `home_view.rs` (`w`, `ci_note` chip), `proto/src/types.rs`, `build.rs` (build-time catalog copy); engine: `engine/rules/doc.py`, `concepts.toml`; docs: `docs/guides/how-lazyagent-works.md` (generated), `docs/guides/home.md` (pointer); tests: generated-doc check, headless page and why-view tests against the fake engine, screenshots |
| R7 | `engine/rules/preview.py`, `engine/methods.py` (`rules_propose`, `rules_decide`), `engine/actionables.py` (`rule_proposal` kind, deep), `mcp_server.py`, `orchestrator_prompt.py` ("Rule proposals"), desktop proposal view (TOML, sentence, preview diff, Accept / Reject), `tests/engine/rules/test_preview.py` |

## 4. Test strategy

- **Oracle (R1).** `tests/engine/rules/oracle.py` is today's `derive()` verbatim. `test_oracle.py` runs every golden derivation case and 10 000 `Facts` from a seeded generator (agents' statuses, PR states, reviews, mergeable, checks, gestures, claims, holds, delegations, trackers, reports, queue items, ages) through both and asserts equal `Derived`. Kept until R2 removes the old code from the engine; the oracle file itself stays as a regression test of the built-in catalog.
- **Byte-identical.** No fixture under `tests/protocol/fixtures/` changes in R1, R3 (no `[*.qa]`), R4 (nothing configured) or R5 (no attestations). R2 adds fixtures for new methods only; the built-in merge becoming an item (Q-R22) is the one intended change, with its fixtures regenerated in R2 and called out in the PR.
- **Language.** Precedence, Kleene tables, unknown never matching, type errors with suggestions, size and depth caps, RE2 subset checks.
- **No network.** Actions artifacts and logs from fixtures through the existing fake `gh`.

## 5. As landed

(empty until R1 merges)
